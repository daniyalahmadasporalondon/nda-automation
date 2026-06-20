function createPlaybookController({ state, playbookList, clauseDetail, renderStudioEmpty, runtime = (typeof PlaybookRuntime !== "undefined" ? PlaybookRuntime : null) }) {
  const TEMPLATE_PREVIEW_CONTEXT = {
    max_term_years: 5,
  };

  // Last server validation result ({ valid, errors }) or null when not yet run /
  // invalidated by an edit. Drives the validation region and gates Publish.
  let lastValidation = null;

  // The draft/publish modules load asynchronously via the runtime bridge. Resolve
  // them once and reuse; every handler awaits this before touching draft helpers.
  let runtimeReadyPromise = null;
  function ensureRuntime() {
    if (!runtime) return Promise.resolve(null);
    if (!runtimeReadyPromise) runtimeReadyPromise = runtime.ready;
    return runtimeReadyPromise;
  }
  function draftHelpers() {
    return runtime?.draft || null;
  }
  function authoringModel() {
    return runtime?.authoring || null;
  }
  function playbookApi() {
    return runtime?.api || null;
  }

  async function loadPlaybook() {
    playbookList.innerHTML = '<div class="playbook-loading">Loading clauses</div>';
    clauseDetail.innerHTML = '<div class="detail-empty">Loading playbook</div>';

    try {
      await ensureRuntime();
      const api = playbookApi();
      const payload = api
        ? await api.loadPlaybook()
        : await fetch("/api/playbook").then(async (response) => {
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || "Playbook could not load");
            return body;
          });

      updatePlaybookStateFromPayload(payload);
      state.selectedClauseId = state.playbookClauses[0]?.id || null;
      if (!state.latestReviewResult && !state.reviewClauses.length) {
        renderStudioEmpty();
      }
      renderPlaybookList();
      renderClauseDetail();
    } catch (error) {
      playbookList.innerHTML = `<div class="playbook-loading">${escapeHtml(error.message)}</div>`;
      clauseDetail.innerHTML = '<div class="detail-empty">Playbook unavailable</div>';
    }
  }

  function renderPlaybookList() {
    const rows = state.playbookClauses
      .map((clause) => {
        const selected = clause.id === state.selectedClauseId ? "selected active" : "";
        const draft = hasClauseDraft(clause.id) ? '<em>Draft</em>' : "";
        const dynamicBadge = isDynamicClause(clause)
          ? '<small class="playbook-row-dynamic">AI-reviewed</small>'
          : "";
        return `
          <button class="playbook-row ${selected}" type="button" data-clause-id="${escapeHtml(clause.id)}" aria-pressed="${selected ? "true" : "false"}">
            <span>
              <strong>${escapeHtml(clause.name)}</strong>
              <small>${escapeHtml(stanceLabel(clause))}</small>
            </span>
            ${dynamicBadge}
            ${draft}
          </button>
        `;
      })
      .join("");

    // A NATIVE clause cannot be user-added: it needs a code-registered checker in
    // checks/registry.CLAUSE_CHECKS, and publish rejects a non-dynamic clause with
    // no checker. So "Add Clause" always creates a DYNAMIC, data-driven, AI-reviewed
    // clause (engine="dynamic"). The new clause is appended to the draft in-memory
    // and only persisted on Save Draft / Publish.
    playbookList.innerHTML = `
      ${rows}
      <div class="playbook-add-clause">
        <button class="secondary" type="button" id="addPlaybookClause">+ Add Clause</button>
      </div>
    `;

    playbookList.querySelectorAll("[data-clause-id]").forEach((row) => {
      row.addEventListener("click", () => {
        state.selectedClauseId = row.dataset.clauseId;
        renderPlaybookList();
        renderClauseDetail();
      });
    });

    const addButton = playbookList.querySelector("#addPlaybookClause");
    if (addButton) {
      addButton.addEventListener("click", addDynamicClause);
    }
  }

  function isDynamicClause(clause) {
    return String((clause && clause.engine) || "native") === "dynamic";
  }

  // Append a fresh DYNAMIC clause scaffold to the draft and select it. Every field
  // the publish lint requires is seeded so the new clause is lint-clean the moment
  // it is created (the author then edits the prose + conditions). A unique id is
  // derived from the running clause list so two adds never collide.
  function addDynamicClause() {
    const clause = newDynamicClauseScaffold();
    state.playbook.clauses = [...(state.playbook.clauses || []), clause];
    state.playbookClauses = state.playbook.clauses;
    state.selectedClauseId = clause.id;
    renderPlaybookList();
    renderClauseDetail();
  }

  function newDynamicClauseScaffold() {
    const existingIds = new Set((state.playbookClauses || []).map((item) => String(item.id || "")));
    let suffix = state.playbookClauses ? state.playbookClauses.length + 1 : 1;
    let id = `custom_clause_${suffix}`;
    while (existingIds.has(id)) {
      suffix += 1;
      id = `custom_clause_${suffix}`;
    }
    return {
      id,
      engine: "dynamic",
      name: "New Clause",
      type: "prohibited",
      requirement: "Describe the standard the AI should judge each document against.",
      preferred_position: "Describe the acceptable / approved language for this clause.",
      check_trigger: "Describe the wording that should trigger this check.",
      acceptable_language: "Describe the language that is acceptable for this clause.",
      search_terms: ["new clause"],
      semantic_signals: [],
      fallback: { redline_action: "delete_paragraph" },
      rules: {
        version: 1,
        clause_type: "prohibited",
        acceptable_position: "Describe the acceptable / approved language for this clause.",
        pass_conditions: [
          {
            id: "clause_absent",
            decision: "pass",
            issue_type: "none",
            description: "The prohibited language is absent or properly carved out.",
            redline_action: "no_change",
          },
        ],
        fail_conditions: [
          {
            id: "clause_present",
            decision: "fail",
            issue_type: "present_but_wrong",
            description: "The prohibited language appears in operative form.",
            redline_action: "delete_paragraph",
          },
        ],
        review_triggers: [
          {
            id: "clause_ambiguous",
            decision: "review",
            issue_type: "unclear",
            description: "The language is ambiguous enough that a human should decide.",
            redline_action: "no_change",
          },
        ],
        evidence_requirements: {
          quote_required: true,
          minimum_evidence_for_pass: 0,
          minimum_evidence_for_fail: 1,
          guidance: "Cite the exact operative wording for failures and the ambiguous wording for review.",
        },
        redline_guidance: {
          default_action: "delete_paragraph",
          drafting_note: "Remove the prohibited language rather than replacing it.",
        },
      },
    };
  }

  function renderClauseDetail() {
    const clause = selectedClause();
    if (!clause) {
      clauseDetail.innerHTML = '<div class="detail-empty">No clause selected</div>';
      return;
    }
    renderTabbedClauseDetail(clause);
  }

  function renderTabbedClauseDetail(clause) {
    const model = authoringModel();
    const panelState = playbookPanelState();
    const activePanel = model
      ? model.resolveActivePanel({
        clauseId: clause.id,
        panelState,
        mutualityPanel: state.playbookMutualityPanel,
      })
      : fallbackActivePanel(clause.id, panelState, state.playbookMutualityPanel);
    const panelActive = (name) => activePanel === name;
    clauseDetail.innerHTML = `
      <form class="playbook-editor playbook-editor-tabbed" id="playbookEditor">
        <div class="admin-head">
          <div>
            <p class="eyebrow">clause ${escapeHtml(clause.id)}</p>
            <h2>Edit Clause: ${escapeHtml(clause.name)}</h2>
          </div>
          <span class="policy-chip ${escapeHtml(clause.type)}">${escapeHtml(stanceLabel(clause))}</span>
        </div>

        ${playbookStatusBanner()}

        <nav class="playbook-subpanel-tabs" aria-label="${escapeHtml(clause.name)} editor sections">
          <button class="${panelActive("policy") ? "active" : ""}" type="button" data-playbook-panel-tab="policy" aria-pressed="${panelActive("policy") ? "true" : "false"}">Policy</button>
          <button class="${panelActive("redline") ? "active" : ""}" type="button" data-playbook-panel-tab="redline" aria-pressed="${panelActive("redline") ? "true" : "false"}">Redline</button>
          <button class="${panelActive("decision") ? "active" : ""}" type="button" data-playbook-panel-tab="decision" aria-pressed="${panelActive("decision") ? "true" : "false"}">Decision Logic</button>
          <button class="${panelActive("audit") ? "active" : ""}" type="button" data-playbook-panel-tab="audit" aria-pressed="${panelActive("audit") ? "true" : "false"}">Audit</button>
        </nav>

        <section class="playbook-subpanel ${panelActive("policy") ? "active" : ""}" data-playbook-panel="policy" ${panelActive("policy") ? "" : "hidden"}>
          <div class="playbook-subpanel-head">
            <h3>Policy</h3>
            <p>Define the ${escapeHtml(clause.name)} rule the review engine should apply.</p>
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
          ${standardPositionControls(clause)}
          ${policyPanelControls(clause)}
          ${triggerTermsControls(clause)}
        </section>

        <section class="playbook-subpanel ${panelActive("redline") ? "active" : ""}" data-playbook-panel="redline" ${panelActive("redline") ? "" : "hidden"}>
          <div class="playbook-subpanel-head">
            <h3>Redline</h3>
            <p>Control the language exported when ${escapeHtml(clause.name)} needs a redline.</p>
          </div>
          ${redlinePanelControls(clause)}
        </section>

        <section class="playbook-subpanel ${panelActive("decision") ? "active" : ""}" data-playbook-panel="decision" ${panelActive("decision") ? "" : "hidden"}>
          <div class="playbook-subpanel-head">
            <h3>Decision Logic</h3>
            <p>Review how ${escapeHtml(clause.name)} is assessed and what evidence appears in audit output.</p>
          </div>
          ${decisionPanelControls(clause)}
        </section>

        <section class="playbook-subpanel ${panelActive("audit") ? "active" : ""}" data-playbook-panel="audit" ${panelActive("audit") ? "" : "hidden"}>
          <div class="playbook-subpanel-head">
            <h3>Audit</h3>
            <p>Inspect the raw rule payload, local draft diff, and version history.</p>
          </div>
          <section class="admin-rules">
            <h3>Raw Engine Rules</h3>
            <pre>${escapeHtml(engineRulesForClause(clause))}</pre>
          </section>
          <section class="admin-rules diff">
            <h3>Draft Modifications Diff</h3>
            <pre id="playbookDraftDiff">${escapeHtml(diffForClause(clause.id) || "No unsaved changes.")}</pre>
          </section>
          ${playbookHistoryPanel()}
        </section>

        <div class="playbook-validation" id="playbookValidation" data-state="idle" aria-live="polite" hidden></div>

        <div class="admin-actions playbook-draft-actions">
          <span class="admin-save-status" id="playbookSaveStatus" aria-live="polite"></span>
          <button class="secondary" type="button" id="discardPlaybookDraft" ${actionAvailabilityForClause(clause).discardDisabled ? "disabled" : ""}>Discard Changes</button>
          <button class="secondary" type="button" id="validatePlaybookButton">Validate Draft</button>
          <button type="submit" id="savePlaybookButton" ${actionAvailabilityForClause(clause).saveDisabled ? "disabled" : ""}>Save Draft</button>
          <button class="primary" type="button" id="publishPlaybookButton" ${actionAvailabilityForClause(clause).publishDisabled ? "disabled" : ""}>Publish Playbook</button>
        </div>
      </form>
    `;

    const editor = clauseDetail.querySelector("#playbookEditor");
    editor.addEventListener("input", handleEditorInput);
    editor.addEventListener("submit", saveDraft);
    clauseDetail.querySelector("#discardPlaybookDraft").addEventListener("click", discardSelectedDraft);
    clauseDetail.querySelector("#validatePlaybookButton").addEventListener("click", validateDraft);
    clauseDetail.querySelector("#publishPlaybookButton").addEventListener("click", publishPlaybook);
    renderValidationState();
    setupSpecialControls(clause);
    setupPlaybookSubpanels();
    setupPlaybookHistoryControls();
  }

  function handleEditorInput(event) {
    const clause = selectedClause();
    if (!clause) return;
    const form = clauseDetail.querySelector("#playbookEditor");
    const data = new FormData(form);
    clause.name = String(data.get("name") || "").trim() || clause.name;
    clause.type = data.get("type") === "prohibited" ? "prohibited" : "required";
    // preferred_position / check_trigger render as editable boxes ONLY for clauses
    // where they are live levers (mutuality, confidential_information, and any other
    // native clause). For governing_law / term_and_survival they are derived from
    // the live levers and shown read-only (no form field), and dynamic clauses
    // author requirement/acceptable_language instead. Guard the read so a clause
    // that does not render these inputs keeps its seeded/derived value instead of
    // being blanked into an invalid (missing-required-field) state.
    if (data.has("preferred_position")) {
      clause.preferred_position = String(data.get("preferred_position") || "").trim();
    }
    if (data.has("check_trigger")) {
      clause.check_trigger = String(data.get("check_trigger") || "").trim();
    }
    templateConfigsForClause(clause).forEach((config) => {
      if (data.has(config.field)) {
        clause[config.field] = String(data.get(config.field) || "").trim();
      }
    });
    removeUnsupportedTemplateFields(clause);
    if (clause.id === "term_and_survival") {
      clause.max_term_years = Math.max(1, Number.parseInt(data.get("max_term_years"), 10) || 5);
    }
    if (clause.id === "governing_law") {
      applyGoverningLawFormData(clause, form, data);
    }
    if (isDynamicClause(clause)) {
      // A dynamic clause's requirement / acceptable language are first-class authored
      // prose (the standard the AI judges against). Read them and the fallback redline
      // out of the form into the model.
      if (data.has("requirement")) {
        clause.requirement = String(data.get("requirement") || "").trim();
      }
      if (data.has("acceptable_language")) {
        clause.acceptable_language = String(data.get("acceptable_language") || "").trim();
      }
      applyDynamicFallback(clause);
    }
    // Decision conditions (pass/fail/review prose, issue_type, redline_action) are
    // now editable for EVERY clause, so read them back for native clauses too. The
    // reader is a no-op when the clause has no condition rows in the DOM.
    applyDynamicConditions(clause);
    syncStructuredRules(clause, event?.target?.name);
    renderDraftState();
  }

  function renderDraftState() {
    const clause = selectedClause();
    const diff = diffForClause(clause.id);
    const diffNode = clauseDetail.querySelector("#playbookDraftDiff");
    const discard = clauseDetail.querySelector("#discardPlaybookDraft");
    const save = clauseDetail.querySelector("#savePlaybookButton");
    const publish = clauseDetail.querySelector("#publishPlaybookButton");
    if (diffNode) diffNode.textContent = diff || "No unsaved changes.";
    if (discard) discard.disabled = !diff;
    renderTemplatePreviewState(clause);
    renderGoverningLawRedlinePreview(clause);
    if (save) save.disabled = !hasAnyDraft() || hasTemplateValidationErrors();
    if (publish) publish.disabled = !canPublish();
    // Editing invalidates a prior validation pass; clear it so the badge can't go stale.
    const model = authoringModel();
    const invalidateValidation = model
      ? model.shouldInvalidateValidation({ validation: lastValidation, hasUnsavedChanges: hasAnyDraft() })
      : Boolean(lastValidation && hasAnyDraft());
    if (invalidateValidation) {
      lastValidation = null;
      renderValidationState();
    }
    updateStatusBannerState();
    renderPlaybookList();
  }

  function fallbackActivePanel(clauseId, panelState, mutualityPanel) {
    const allowed = new Set(["policy", "redline", "decision", "audit"]);
    const savedPanel = panelState[clauseId] || (clauseId === "mutuality" ? mutualityPanel : "");
    return allowed.has(savedPanel) ? savedPanel : "policy";
  }

  function actionAvailabilityForClause(clause) {
    const publishable = canPublish();
    const model = authoringModel();
    if (!model) {
      return {
        discardDisabled: !hasClauseDraft(clause.id),
        saveDisabled: !hasAnyDraft() || hasTemplateValidationErrors(),
        publishDisabled: !publishable,
      };
    }
    return model.actionAvailability({
      clauseHasDraft: hasClauseDraft(clause.id),
      hasUnsavedChanges: hasAnyDraft(),
      hasTemplateValidationErrors: hasTemplateValidationErrors(),
      canPublish: publishable,
    });
  }

  // Patch the version banner's draft-state class + note in place, so live edits
  // flip it to the unsaved-changes look without a full editor re-render.
  function updateStatusBannerState() {
    const banner = clauseDetail.querySelector(".playbook-version-banner");
    if (!banner) return;
    const helpers = draftHelpers();
    const dirty = hasAnyDraft();
    const draftAhead = helpers ? helpers.draftDiffersFromActive(state.draftMeta, state.activePlaybook) : false;
    const status = authoringModel()
      ? authoringModel().draftStatus({ hasUnsavedChanges: dirty, draftAhead })
      : fallbackDraftStatus(dirty, draftAhead);
    banner.dataset.draftState = status.state;
    const draftNote = banner.querySelector(".playbook-version-card.draft small");
    if (draftNote) draftNote.textContent = status.note;
    const draftEyebrow = banner.querySelector(".playbook-version-card.draft .eyebrow");
    if (draftEyebrow) {
      const hasDot = Boolean(draftEyebrow.querySelector(".playbook-dirty-dot"));
      if (status.showDirtyDot && !hasDot) {
        draftEyebrow.insertAdjacentHTML("beforeend", ' <span class="playbook-dirty-dot" aria-hidden="true"></span>');
      } else if (!status.showDirtyDot && hasDot) {
        draftEyebrow.querySelector(".playbook-dirty-dot").remove();
      }
    }
  }

  // Banner above the editor that distinguishes the active published Playbook from
  // the working draft, with version/hash labels and an unpublished-changes hint.
  function playbookStatusBanner() {
    const helpers = draftHelpers();
    const active = state.activePlaybook || null;
    const draft = state.draftMeta || null;
    // Human-readable headline (e.g. "Published Jun 4, 2026, 11:09 PM") with a
    // subtle short fingerprint and the full raw id tucked into a hover tooltip.
    const activeLabel = helpers ? helpers.friendlyVersionLabel(active, "active") : "";
    const draftLabel = helpers ? helpers.friendlyVersionLabel(draft, "draft") : "";
    const dirty = hasAnyDraft();
    const draftAhead = helpers ? helpers.draftDiffersFromActive(draft, active) : false;
    const status = authoringModel()
      ? authoringModel().draftStatus({ hasUnsavedChanges: dirty, draftAhead })
      : fallbackDraftStatus(dirty, draftAhead);
    return `
      <section class="playbook-version-banner" data-draft-state="${escapeHtml(status.state)}" aria-label="Playbook version status">
        <article class="playbook-version-card active">
          <p class="eyebrow">Active published</p>
          <strong${versionTooltipAttr(active)}>${escapeHtml(activeLabel || "Not yet published")}</strong>
          ${versionFingerprint(active)}
          <small>Used by the review engine right now.</small>
        </article>
        <article class="playbook-version-card draft">
          <p class="eyebrow">Working draft${status.showDirtyDot ? " <span class=\"playbook-dirty-dot\" aria-hidden=\"true\"></span>" : ""}</p>
          <strong${versionTooltipAttr(draft)}>${escapeHtml(draftLabel || "No saved draft yet")}</strong>
          ${versionFingerprint(draft)}
          <small>${escapeHtml(status.note)}</small>
        </article>
      </section>
    `;
  }

  function fallbackDraftStatus(dirty, draftAhead) {
    if (dirty) {
      return {
        state: "editing",
        note: "Unsaved changes - Save Draft to keep them.",
        showDirtyDot: true,
      };
    }
    if (draftAhead) {
      return {
        state: "ahead",
        note: "Saved draft is ahead of the active version - Publish to make it live.",
        showDirtyDot: false,
      };
    }
    return {
      state: "in-sync",
      note: "Matches the active published version.",
      showDirtyDot: false,
    };
  }

  // Subtle short-hash fingerprint line under a version headline (omitted when no
  // hash is known), so power users still get a stable identifier at a glance.
  function versionFingerprint(block) {
    const helpers = draftHelpers();
    if (!helpers) return "";
    const fingerprint = helpers.shortHash(helpers.hashOf(block));
    if (!fingerprint) return "";
    return `<span class="playbook-version-fingerprint">#${escapeHtml(fingerprint)}</span>`;
  }

  // title="" tooltip carrying the full raw id + hash for power users, without
  // cluttering the visible label. Returns "" (no attribute) when nothing to show.
  function versionTooltipAttr(block) {
    const helpers = draftHelpers();
    if (!helpers) return "";
    const rawId = helpers.rawVersionId(block);
    const hash = helpers.hashOf(block);
    const parts = [];
    if (rawId) parts.push(rawId);
    if (hash) parts.push(String(hash));
    if (!parts.length) return "";
    return ` title="${escapeHtml(parts.join("\n"))}"`;
  }

  // Publish is allowed only when the draft is saved (no unsaved edits), the draft
  // actually differs from the active version, and no validation errors are pending.
  function canPublish() {
    const helpers = draftHelpers();
    if (!helpers) return false;
    const draftAhead = helpers.draftDiffersFromActive(state.draftMeta, state.activePlaybook);
    const model = authoringModel();
    if (!model) {
      if (hasAnyDraft() || hasTemplateValidationErrors()) return false;
      if (lastValidation && !lastValidation.valid) return false;
      return draftAhead;
    }
    return model.canPublishDraft({
      hasUnsavedChanges: hasAnyDraft(),
      hasTemplateValidationErrors: hasTemplateValidationErrors(),
      validation: lastValidation,
      draftAhead,
    });
  }

  // Format a confidence in [0,1] as a percent badge, or "" when not reported.
  function confidenceLabel(confidence) {
    if (typeof confidence !== "number" || !Number.isFinite(confidence)) return "";
    const pct = Math.round(Math.max(0, Math.min(1, confidence)) * 100);
    return `<span class="playbook-validation-confidence">${pct}% confidence</span>`;
  }

  // Render the ADVISORY Layer-2 semantic-lint warnings as a block that is visually
  // distinct from blocking errors. Returns "" when there are no warnings so the
  // error/valid markup is unchanged in that (common) case. Warnings never block
  // publishing -- they are an advisory channel only.
  function renderWarningsBlock(warnings) {
    const list = Array.isArray(warnings) ? warnings : [];
    if (!list.length) return "";
    const items = list
      .map((warning) => {
        const where = warning.clause_id
          ? `<span class="playbook-validation-where">${escapeHtml(clauseNameForId(warning.clause_id))}${warning.field ? ` &middot; ${escapeHtml(warning.field)}` : ""}</span>`
          : "";
        return `<li>${where}<span>${escapeHtml(warning.message)}</span>${confidenceLabel(warning.confidence)}</li>`;
      })
      .join("");
    const heading = list.length === 1 ? "Advisory warning" : "Advisory warnings";
    return `
      <div class="playbook-validation-warnings" data-advisory="true">
        <p class="playbook-validation-warnings-title">${heading} (does not block publishing):</p>
        <ul class="playbook-validation-list">${items}</ul>
      </div>
    `;
  }

  // Render the validation region from the last validation result. Hidden until a
  // validation has run; shows a success note or a list of errors, plus any advisory
  // (Layer-2 semantic-lint) warnings rendered distinctly below.
  function renderValidationState() {
    const region = clauseDetail.querySelector("#playbookValidation");
    if (!region) return;
    const view = authoringModel()
      ? authoringModel().validationView(lastValidation)
      : null;
    if (view?.hidden || (!view && !lastValidation)) {
      region.hidden = true;
      region.dataset.state = "idle";
      region.innerHTML = "";
      return;
    }
    if (view) {
      region.hidden = false;
      region.dataset.state = view.state;
      const warningsBlock = renderWarningsBlock(view.warnings);
      region.dataset.hasWarnings = warningsBlock ? "true" : "false";
      if (view.state === "valid") {
        region.innerHTML = '<p class="playbook-validation-ok">Draft passed validation.</p>' + warningsBlock;
        return;
      }
      const items = view.errors
        .map((error) => {
          const where = error.clause_id
            ? `<span class="playbook-validation-where">${escapeHtml(clauseNameForId(error.clause_id))}${error.field ? ` &middot; ${escapeHtml(error.field)}` : ""}</span>`
            : "";
          return `<li>${where}<span>${escapeHtml(error.message)}</span></li>`;
        })
        .join("");
      region.innerHTML = `
        <p class="playbook-validation-title">${escapeHtml(view.title)}</p>
        <ul class="playbook-validation-list">${items}</ul>
      ` + warningsBlock;
      return;
    }
    region.hidden = false;
    const warningsBlock = renderWarningsBlock(lastValidation.warnings);
    region.dataset.hasWarnings = warningsBlock ? "true" : "false";
    if (lastValidation.valid) {
      region.dataset.state = "valid";
      region.innerHTML = '<p class="playbook-validation-ok">Draft passed validation.</p>' + warningsBlock;
      return;
    }
    region.dataset.state = "invalid";
    const items = lastValidation.errors
      .map((error) => {
        const where = error.clause_id
          ? `<span class="playbook-validation-where">${escapeHtml(clauseNameForId(error.clause_id))}${error.field ? ` &middot; ${escapeHtml(error.field)}` : ""}</span>`
          : "";
        return `<li>${where}<span>${escapeHtml(error.message)}</span></li>`;
      })
      .join("");
    region.innerHTML = `
      <p class="playbook-validation-title">Resolve ${lastValidation.errors.length === 1 ? "this issue" : "these issues"} before publishing:</p>
      <ul class="playbook-validation-list">${items}</ul>
    ` + warningsBlock;
  }

  // Friendly clause name for a validation error's clause_id (falls back to the id).
  function clauseNameForId(clauseId) {
    const clause = state.playbookClauses.find((item) => item.id === clauseId);
    return clause?.name || clauseId;
  }

  function setupSpecialControls(clause) {
    if (clause.id === "term_and_survival") {
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
    if (clause.id === "governing_law") {
      const addButton = clauseDetail.querySelector("#addGoverningLaw");
      const input = clauseDetail.querySelector("#governingLawInput");
      const forumInput = clauseDetail.querySelector("#governingLawForumInput");
      if (addButton && input) {
        addButton.addEventListener("click", () => {
          const value = input.value.trim();
          if (!value) return;
          const forum = forumInput ? forumInput.value.trim() : "";
          clause.approved_laws = dedupeList([...(clause.approved_laws || []), value]);
          clause.law_phrases = { ...(clause.law_phrases || {}), [value]: value };
          if (!clause.preferred_law) clause.preferred_law = value;
          // Seed the new option's authored court/forum so syncStructuredRules'
          // merge writes it onto the freshly-built option object. The publish
          // lint requires a non-empty forum_jurisdiction, so authoring the court
          // here at add-time is the happy path.
          clause._forumByOptionId = {
            ...(clause._forumByOptionId || {}),
            [optionIdForLaw(value)]: forum,
          };
          syncStructuredRules(clause);
          input.value = "";
          if (forumInput) forumInput.value = "";
          renderClauseDetail();
        });
        input.addEventListener("keydown", (event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          addButton.click();
        });
      }
      clauseDetail.querySelectorAll("[data-remove-governing-law]").forEach((button) => {
        button.addEventListener("click", () => {
          const index = Number.parseInt(button.dataset.removeGoverningLaw, 10);
          const approved = clause.approved_laws || [];
          if (!Number.isInteger(index) || approved.length <= 1) return;
          const nextApproved = approved.filter((_law, lawIndex) => lawIndex !== index);
          clause.approved_laws = nextApproved;
          if (!nextApproved.includes(clause.preferred_law)) {
            clause.preferred_law = nextApproved[0] || "";
          }
          syncStructuredRules(clause);
          renderClauseDetail();
        });
      });
      clauseDetail.querySelectorAll("[data-preferred-governing-law]").forEach((input) => {
        input.addEventListener("change", () => {
          const index = Number.parseInt(input.value, 10);
          const approved = clause.approved_laws || [];
          clause.preferred_law = approved[index] || approved[0] || "";
          syncStructuredRules(clause);
          renderClauseDetail();
        });
      });
    }
    // Trigger-term chips and decision-condition editing are now available for
    // EVERY clause (native + dynamic). The redline-action / fallback-wording
    // handlers inside are self-guarded by element presence (only the dynamic
    // redline panel renders #dynamicRedlineAction), so this is safe to run for
    // native clauses too.
    setupClauseEditorControls(clause);
  }

  function setupClauseEditorControls(clause) {
    // --- Trigger-term chips (search_terms / semantic_signals) ---
    const chipAdders = [
      { buttonId: "addDynamicSearchTerm", inputId: "dynamicSearchTermInput", field: "search_terms" },
      { buttonId: "addDynamicSemanticSignal", inputId: "dynamicSemanticSignalInput", field: "semantic_signals" },
    ];
    chipAdders.forEach(({ buttonId, inputId, field }) => {
      const button = clauseDetail.querySelector(`#${buttonId}`);
      const input = clauseDetail.querySelector(`#${inputId}`);
      if (!button || !input) return;
      const add = () => {
        const value = input.value.trim();
        if (!value) return;
        clause[field] = dedupeList([...(clause[field] || []), value]);
        input.value = "";
        renderClauseDetail();
      };
      button.addEventListener("click", add);
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        add();
      });
    });
    clauseDetail.querySelectorAll("[data-remove-chip]").forEach((button) => {
      button.addEventListener("click", () => {
        const kind = button.dataset.removeChip;
        const field = kind === "semantic-signal" ? "semantic_signals" : "search_terms";
        const value = button.dataset.chipValue;
        clause[field] = (clause[field] || []).filter((item) => item !== value);
        renderClauseDetail();
      });
    });

    // --- Redline action / wording ---
    const redlineSelect = clauseDetail.querySelector("#dynamicRedlineAction");
    if (redlineSelect) {
      redlineSelect.addEventListener("change", () => {
        applyDynamicFallback(clause);
        renderClauseDetail();
      });
    }

    // --- Decision conditions: add / remove ---
    clauseDetail.querySelectorAll("[data-add-condition]").forEach((button) => {
      button.addEventListener("click", () => {
        const field = button.dataset.addCondition;
        addDynamicCondition(clause, field);
        renderClauseDetail();
      });
    });
    clauseDetail.querySelectorAll("[data-remove-condition]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("[data-condition-field]");
        if (!row) return;
        const field = row.dataset.conditionField;
        const index = Number.parseInt(row.dataset.conditionIndex, 10);
        const list = (clause.rules && clause.rules[field]) || [];
        if (Number.isInteger(index)) {
          clause.rules[field] = list.filter((_item, itemIndex) => itemIndex !== index);
        }
        renderClauseDetail();
      });
    });
  }

  function addDynamicCondition(clause, field) {
    if (!clause.rules || typeof clause.rules !== "object") return;
    const list = Array.isArray(clause.rules[field]) ? clause.rules[field] : [];
    const decision = field === "fail_conditions" ? "fail" : field === "review_triggers" ? "review" : "pass";
    const issueType = (ISSUE_TYPES_BY_LIST[field] || ["none"])[0];
    const base = {
      id: `${field}_${list.length + 1}`,
      decision,
      issue_type: issueType,
      description: "",
    };
    if (field !== "pass_conditions") {
      base.redline_action = clause.type === "prohibited" ? "delete_paragraph" : "replace_paragraph";
    } else {
      base.redline_action = "no_change";
    }
    clause.rules[field] = [...list, base];
  }

  // Mirror the dynamic fallback select + wording textarea into clause.fallback.
  function applyDynamicFallback(clause) {
    const select = clauseDetail.querySelector("#dynamicRedlineAction");
    const wordingArea = clauseDetail.querySelector('[name="fallback_wording"]');
    const action = select ? String(select.value || "no_change") : "no_change";
    const fallback = clause.fallback && typeof clause.fallback === "object" ? { ...clause.fallback } : {};
    fallback.redline_action = action;
    const wording = wordingArea ? String(wordingArea.value || "").trim() : String(fallback.wording || "").trim();
    if (action === "replace_paragraph" || action === "insert_after_paragraph") {
      fallback.wording = wording;
    } else {
      delete fallback.wording;
    }
    clause.fallback = fallback;
  }

  // Read every dynamic-condition control out of the DOM into clause.rules. Called
  // from handleEditorInput so edits to condition prose / issue_type / redline_action
  // are captured into the model the same way the static fields are.
  function applyDynamicConditions(clause) {
    if (!clause.rules || typeof clause.rules !== "object") return;
    ["pass_conditions", "fail_conditions", "review_triggers"].forEach((field) => {
      const decision = field === "fail_conditions" ? "fail" : field === "review_triggers" ? "review" : "pass";
      const rows = [...clauseDetail.querySelectorAll(`[data-condition-field="${field}"]`)];
      if (!rows.length) return;
      clause.rules[field] = rows.map((row) => {
        const idInput = row.querySelector("[data-condition-id]");
        const descInput = row.querySelector("[data-condition-description]");
        const issueSelect = row.querySelector("[data-condition-issue]");
        const redlineSelect = row.querySelector("[data-condition-redline]");
        const condition = {
          id: idInput ? String(idInput.value || "").trim() : "",
          decision,
          issue_type: issueSelect ? String(issueSelect.value || "") : "",
          description: descInput ? String(descInput.value || "").trim() : "",
        };
        if (field !== "pass_conditions") {
          condition.redline_action = redlineSelect ? String(redlineSelect.value || "no_change") : "no_change";
        } else {
          condition.redline_action = "no_change";
        }
        return condition;
      });
    });
  }

  function setupPlaybookSubpanels() {
    const tabs = [...clauseDetail.querySelectorAll("[data-playbook-panel-tab]")];
    const panels = [...clauseDetail.querySelectorAll("[data-playbook-panel]")];
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        const target = tab.dataset.playbookPanelTab;
        const clause = selectedClause();
        if (clause) {
          playbookPanelState()[clause.id] = target;
          if (clause.id === "mutuality") state.playbookMutualityPanel = target;
          const model = authoringModel();
          if (model) {
            const next = model.setClausePanel({
              clauseId: clause.id,
              panel: target,
              panelState: playbookPanelState(),
              mutualityPanel: state.playbookMutualityPanel,
            });
            state.playbookClausePanels = next.panelState;
            state.playbookMutualityPanel = next.mutualityPanel;
          }
        }
        tabs.forEach((item) => {
          const active = item === tab;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        panels.forEach((panel) => {
          const active = panel.dataset.playbookPanel === target;
          panel.classList.toggle("active", active);
          panel.hidden = !active;
        });
      });
    });
  }

  function playbookPanelState() {
    if (!state.playbookClausePanels || typeof state.playbookClausePanels !== "object") {
      state.playbookClausePanels = {};
    }
    return state.playbookClausePanels;
  }

  // Save Draft persists the working clauses to the server-side draft only; the
  // active published Playbook is untouched. Bound to the form submit.
  async function saveDraft(event) {
    if (event) event.preventDefault();
    const status = clauseDetail.querySelector("#playbookSaveStatus");
    const saveButton = clauseDetail.querySelector("#savePlaybookButton");
    sanitizePlaybookTemplatesForSave();
    const templateError = templateValidationSummary();
    if (templateError) {
      if (status) status.textContent = templateError;
      if (saveButton) saveButton.disabled = true;
      return;
    }
    if (status) status.textContent = "Saving draft...";
    if (saveButton) saveButton.disabled = true;

    try {
      await ensureRuntime();
      const api = playbookApi();
      if (!api) throw new Error("Draft tools are still loading. Try again in a moment.");
      const payload = await api.saveDraft(state.playbook, { activeMeta: activeMetadata() });
      // The saved draft is the new diff baseline; refresh labels from the response.
      state.savedPlaybook = clonePlaybook(state.playbook);
      applyActiveBlock(extractBlock(payload, "active", state.activePlaybook));
      applyDraftBlock(extractBlock(payload, "draft", state.draftMeta));
      renderPlaybookList();
      renderClauseDetail();
      // Re-render rebuilds #clauseDetail, so set the status on the fresh node.
      setSaveStatus("Draft saved.");
    } catch (error) {
      if (status) status.textContent = error.message;
      if (saveButton) saveButton.disabled = !hasAnyDraft() || hasTemplateValidationErrors();
    }
  }

  // Validate Draft asks the backend to check the working clauses without saving,
  // then surfaces any errors in the validation region. Does not mutate the draft.
  async function validateDraft() {
    const status = clauseDetail.querySelector("#playbookSaveStatus");
    const button = clauseDetail.querySelector("#validatePlaybookButton");
    sanitizePlaybookTemplatesForSave();
    if (status) status.textContent = "Validating draft...";
    if (button) button.disabled = true;
    try {
      await ensureRuntime();
      const api = playbookApi();
      const helpers = draftHelpers();
      if (!api || !helpers) throw new Error("Draft tools are still loading. Try again in a moment.");
      const payload = await api.validateDraft(state.playbook);
      lastValidation = helpers.normalizeValidation(payload);
      if (status) status.textContent = helpers.validationSummary(lastValidation);
      renderValidationState();
      const publish = clauseDetail.querySelector("#publishPlaybookButton");
      if (publish) publish.disabled = !canPublish();
    } catch (error) {
      if (status) status.textContent = error.message;
    } finally {
      if (button) button.disabled = false;
    }
  }

  // Publish promotes the saved draft to the active Playbook the engine uses. We
  // require a clean (saved) draft so what publishes matches what the editor shows.
  async function publishPlaybook() {
    const status = clauseDetail.querySelector("#playbookSaveStatus");
    const button = clauseDetail.querySelector("#publishPlaybookButton");
    if (hasAnyDraft()) {
      if (status) status.textContent = "Save the draft before publishing.";
      return;
    }
    if (lastValidation && !lastValidation.valid) {
      if (status) status.textContent = "Resolve validation issues before publishing.";
      return;
    }
    if (status) status.textContent = "Publishing playbook...";
    if (button) button.disabled = true;
    try {
      await ensureRuntime();
      const api = playbookApi();
      if (!api) throw new Error("Draft tools are still loading. Try again in a moment.");
      const payload = await api.publishPlaybook(state.playbook, {
        activeMeta: activeMetadata(),
        // When a server draft is outstanding (the save-then-publish flow), publish
        // THAT draft by id; a direct playbook-object publish 409s while a draft exists.
        draftId: draftMetadataId(),
      });
      applyActiveBlock(extractBlock(payload, "active", state.activePlaybook));
      // Publishing clears the server draft; reflect the now-in-sync state by
      // re-baselining the draft block to the freshly published active version.
      const publishedActive = extractBlock(payload, "active", state.activePlaybook);
      applyDraftBlock(clonePlaybook(publishedActive));
      state.savedPlaybook = clonePlaybook(state.playbook);
      state.playbookHistory = Array.isArray(payload?.history) ? payload.history : state.playbookHistory;
      lastValidation = null;
      renderPlaybookList();
      renderClauseDetail();
      // Re-render rebuilds #clauseDetail, so set the status on the fresh node.
      setSaveStatus("Playbook published.");
    } catch (error) {
      if (status) status.textContent = error.message;
      if (button) button.disabled = !canPublish();
    }
  }

  // Write to the (possibly freshly re-rendered) save-status region.
  function setSaveStatus(message) {
    const node = clauseDetail.querySelector("#playbookSaveStatus");
    if (node) node.textContent = message;
  }

  // Pull a named block ({active}/{draft}) out of an API response. Each block is
  // `{ playbook, metadata, ... }`. When the backend omits the block (e.g. draft is
  // null after publish/discard) keep the current state value.
  function extractBlock(payload, key, fallback) {
    if (payload && typeof payload === "object" && payload[key] && typeof payload[key] === "object") {
      return payload[key];
    }
    return fallback;
  }

  // The active block's metadata, used for opt-in optimistic-concurrency hints.
  function activeMetadata() {
    const block = state.activePlaybook;
    return block && typeof block.metadata === "object" ? block.metadata : null;
  }

  // The id of the outstanding server draft (empty when none). Threaded into publish
  // so the saved draft is the publish target.
  function draftMetadataId() {
    const meta = state.draftMeta && typeof state.draftMeta.metadata === "object" ? state.draftMeta.metadata : null;
    return meta && meta.draft_id ? String(meta.draft_id) : "";
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

  async function restorePlaybookVersion(historyId) {
    const status = clauseDetail.querySelector("#playbookSaveStatus");
    if (hasAnyDraft()) {
      if (status) status.textContent = "Discard unsaved changes before restoring a saved version.";
      return;
    }
    if (status) status.textContent = "Restoring playbook version...";

    try {
      await ensureRuntime();
      const api = playbookApi();
      const payload = api
        ? await api.restoreVersion(historyId, "admin")
        : await fetch("/api/playbook/restore", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ history_id: historyId, actor: "admin" }),
          }).then(async (response) => {
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || "Playbook version could not be restored");
            return body;
          });
      updatePlaybookStateFromPayload(payload);
      if (!selectedClause()) state.selectedClauseId = state.playbookClauses[0]?.id || null;
      lastValidation = null;
      if (status) status.textContent = "Playbook version restored and published.";
      renderPlaybookList();
      renderClauseDetail();
    } catch (error) {
      if (status) status.textContent = error.message;
    }
  }

  function selectedClause() {
    return state.playbookClauses.find((item) => item.id === state.selectedClauseId);
  }

  function savedClause(clauseId) {
    return (state.savedPlaybook?.clauses || []).find((item) => item.id === clauseId);
  }

  function updatePlaybookStateFromPayload(payload) {
    const helpers = draftHelpers();
    if (helpers) {
      // New draft/publish contract: keep the active published block separate from
      // the editable draft. The form binds to the draft; `savedPlaybook` is the
      // draft baseline used for the unsaved-changes diff.
      const normalized = helpers.normalizePlaybookResponse(payload);
      state.activePlaybook = normalized.active;
      state.draftMeta = normalized.draft;
      const draftPlaybook = helpers.playbookOf(normalized.draft) || { clauses: [] };
      state.playbook = clonePlaybook(draftPlaybook);
      state.savedPlaybook = clonePlaybook(draftPlaybook);
      state.playbookClauses = state.playbook.clauses || [];
      state.playbookHistory = normalized.history;
      return;
    }
    // Legacy fallback (runtime modules unavailable): single playbook acts as both.
    const playbook = payload?.playbook && typeof payload.playbook === "object" ? payload.playbook : payload;
    state.playbook = clonePlaybook(playbook);
    state.savedPlaybook = clonePlaybook(playbook);
    state.activePlaybook = { playbook: clonePlaybook(playbook), version: null, hash: null };
    state.draftMeta = { playbook: clonePlaybook(playbook), version: null, hash: null };
    state.playbookClauses = state.playbook.clauses || [];
    state.playbookHistory = Array.isArray(payload?.history) ? payload.history : [];
  }

  // After a Save Draft / Publish round-trip the backend returns an updated block.
  // Merge it into state without disturbing the in-memory working clauses (which the
  // user may keep editing). Used to refresh version/hash labels post-action.
  function applyDraftBlock(block) {
    if (block && typeof block === "object") state.draftMeta = block;
  }
  function applyActiveBlock(block) {
    if (block && typeof block === "object") state.activePlaybook = block;
  }

  function hasClauseDraft(clauseId) {
    return Boolean(diffForClause(clauseId));
  }

  function hasAnyDraft() {
    return state.playbookClauses.some((clause) => hasClauseDraft(clause.id));
  }

  function diffForClause(clauseId) {
    const clause = state.playbookClauses.find((item) => item.id === clauseId);
    if (!clause) return "";
    const saved = savedClause(clauseId);
    // A clause with no saved counterpart is a NEWLY-ADDED (Add-Clause) clause: it is
    // entirely an unsaved change, so report a diff against an empty baseline. Without
    // this, a freshly-added dynamic clause registers no diff -> hasAnyDraft() stays
    // false -> Save Draft never enables and the new clause can never be published.
    if (!saved) {
      return `new clause:\n+ ${formatDiffValue(clause.name || clause.id || clauseId)} (added)`;
    }
    const fields = [
      "name",
      "type",
      "preferred_position",
      "check_trigger",
      ...templateConfigsForClause(clause).map((config) => config.field),
      "max_term_years",
      "longer_survival_carve_out_terms",
      "approved_laws",
      "preferred_law",
      "law_phrases",
      // Dynamic-clause authoring fields, so edits to an authored clause's prose,
      // trigger terms, redline fallback, and decision conditions count as unsaved
      // changes (the same way the native-clause fields above do).
      "requirement",
      "acceptable_language",
      "search_terms",
      "semantic_signals",
      "fallback",
      "rules.clause_type",
      "rules.acceptable_position",
      "rules.approved_options",
      "rules.redline_guidance",
      "rules.pass_conditions",
      "rules.fail_conditions",
      "rules.review_triggers",
    ];
    return fields
      .filter((field) => stableJson(valueAt(clause, field)) !== stableJson(valueAt(saved, field)))
      .map((field) => `${field}:\n- ${formatDiffValue(valueAt(saved, field))}\n+ ${formatDiffValue(valueAt(clause, field))}`)
      .join("\n\n");
  }

  function policyPanelControls(clause) {
    if (clause.id === "term_and_survival") {
      return termSurvivalPolicyControls(clause);
    }
    if (clause.id === "governing_law") {
      return governingLawPolicyControls(clause);
    }
    if (isDynamicClause(clause)) {
      return dynamicPolicyControls(clause);
    }
    return "";
  }

  // Authoring controls for a dynamic (AI-reviewed) clause's prose: the standard the
  // AI judges against (requirement), the acceptable/approved language, and the
  // trigger terms (search_terms / semantic_signals) that surface the clause to the
  // detector. These are the data the AI engine reads to assess a clause type the
  // code has never seen.
  function dynamicPolicyControls(clause) {
    return `
      <section class="admin-special" data-dynamic-policy="1">
        <h3>AI Review Standard</h3>
        <p class="admin-muted">The standard the AI judges each document against, and the language it should accept.</p>
        ${textArea("Requirement (the standard the AI judges against)", "requirement", String(clause.requirement || ""), 3)}
        ${textArea("Acceptable / Approved Language", "acceptable_language", String(clause.acceptable_language || ""), 3)}
      </section>
    `;
  }

  // The governing_law and term_and_survival clauses RE-DERIVE preferred_position
  // and check_trigger from their live levers (the approved-jurisdiction list /
  // max_term_years) on every AI-packet build (playbook_rules._normalize_*). So a
  // free-text edit to these boxes is silently overwritten -- editable-but-inert.
  // For those two clauses we show the derived text read-only and point the author
  // at the real lever; for every other clause the boxes are genuinely live.
  const DERIVED_STANDARD_CLAUSES = {
    governing_law: "the Approved Governing Laws list below",
    term_and_survival: "the Ordinary Confidentiality Cap (years) below",
  };

  function standardPositionControls(clause) {
    const lever = DERIVED_STANDARD_CLAUSES[clause.id];
    if (lever) {
      return `
        <section class="admin-special" data-derived-standard="1">
          <h3>Standard Position (derived)</h3>
          <p class="admin-muted">For this clause the preferred position and check trigger are generated from ${escapeHtml(lever)} on every review. They cannot be edited as free text here -- change the live lever instead.</p>
          <label class="admin-field compact"><span>Preferred Standard Position (read-only, derived)</span>
            <textarea rows="3" readonly disabled>${escapeHtml(preferredPosition(clause))}</textarea>
          </label>
          <label class="admin-field compact"><span>Check Trigger Position (read-only, derived)</span>
            <textarea rows="3" readonly disabled>${escapeHtml(checkTrigger(clause))}</textarea>
          </label>
        </section>
      `;
    }
    if (isDynamicClause(clause)) {
      // Dynamic clauses author requirement / acceptable_language in their own
      // section (dynamicPolicyControls); preferred_position / check_trigger are
      // not separate live levers for them, so don't render duplicate boxes.
      return "";
    }
    return `
      ${textArea("Preferred Standard Position", "preferred_position", preferredPosition(clause), 3)}
      ${textArea("Check Trigger Position", "check_trigger", checkTrigger(clause), 3)}
    `;
  }

  // Editable trigger-term chips (search_terms / semantic_signals) for EVERY
  // clause -- native and dynamic alike. search_terms drive the deterministic
  // detector (e.g. mutuality.py reads them); semantic_signals ride into the AI
  // packet. At least one search term is required (the publish gate enforces it).
  function triggerTermsControls(clause) {
    const searchTerms = chipList(clause.search_terms || [], "search-term");
    const semanticSignals = chipList(clause.semantic_signals || [], "semantic-signal");
    return `
      <section class="admin-special" data-dynamic-triggers="1">
        <h3>Trigger Terms</h3>
        <p class="admin-muted">Words and phrases that surface this clause to the detector and AI packet. At least one search term is required.</p>
        <label class="admin-field compact"><span>Search Terms</span></label>
        <div class="admin-chip-row" data-chip-row="search-term">${searchTerms || '<span class="admin-muted">No search terms yet</span>'}</div>
        <div class="admin-inline-add">
          <input type="text" id="dynamicSearchTermInput" placeholder="e.g. non-compete">
          <button class="secondary" type="button" id="addDynamicSearchTerm">Add</button>
        </div>
        <label class="admin-field compact"><span>Semantic Signals (optional)</span></label>
        <div class="admin-chip-row" data-chip-row="semantic-signal">${semanticSignals || '<span class="admin-muted">No semantic signals yet</span>'}</div>
        <div class="admin-inline-add">
          <input type="text" id="dynamicSemanticSignalInput" placeholder="e.g. restraint on competition">
          <button class="secondary" type="button" id="addDynamicSemanticSignal">Add</button>
        </div>
      </section>
    `;
  }

  function chipList(values, kind) {
    return (values || [])
      .map((item) => `
        <button class="admin-chip removable" type="button" data-remove-chip="${escapeHtml(kind)}" data-chip-value="${escapeHtml(item)}">
          ${escapeHtml(item)} <span aria-hidden="true">x</span>
        </button>
      `)
      .join("");
  }

  function redlinePanelControls(clause) {
    if (isDynamicClause(clause)) {
      return dynamicRedlineControls(clause);
    }
    const controls = [redlineTemplateEditors(clause)];
    if (clause.id === "confidential_information") {
      controls.push(templateEditorBlock(clause, standardExclusionsTemplateConfig()));
    }
    if (clause.id === "governing_law") {
      controls.push(governingLawRedlineControls(clause));
    }
    const html = controls.filter(Boolean).join("");
    return html || `
      <section class="admin-special">
        <h3>No Editable Redline Settings</h3>
        <p class="admin-muted">This clause uses generated redline behavior from the review engine.</p>
      </section>
    `;
  }

  function decisionPanelControls(clause) {
    return `
      ${checkerVisibilityPanel(clause)}
      ${clause.id === "term_and_survival" ? termSurvivalDecisionControls() : ""}
      ${dynamicDecisionControls(clause)}
      ${sharedContextControls(clause)}
    `;
  }

  // The redline the engine applies when a dynamic clause is flagged. A dynamic
  // clause carries its wording in fallback.wording (it cannot carry a
  // redline_template). The action set mirrors the backend's AI_ASSESSMENT actions;
  // delete_paragraph / no_change need no wording, replace / insert do.
  const DYNAMIC_REDLINE_ACTIONS = [
    { value: "no_change", label: "No change" },
    { value: "delete_paragraph", label: "Delete paragraph (remove prohibited language)" },
    { value: "replace_paragraph", label: "Replace paragraph (substitute approved wording)" },
    { value: "insert_after_paragraph", label: "Insert after paragraph (add required wording)" },
  ];

  function dynamicRedlineControls(clause) {
    const fallback = clause.fallback && typeof clause.fallback === "object" ? clause.fallback : {};
    const action = String(fallback.redline_action || "no_change");
    const wording = String(fallback.wording || "");
    const wordingNeeded = action === "replace_paragraph" || action === "insert_after_paragraph";
    const options = DYNAMIC_REDLINE_ACTIONS
      .map((opt) => `<option value="${escapeHtml(opt.value)}" ${opt.value === action ? "selected" : ""}>${escapeHtml(opt.label)}</option>`)
      .join("");
    return `
      <section class="admin-special" data-dynamic-redline="1">
        <h3>Redline Action</h3>
        <p class="admin-muted">What the review engine does to a flagged paragraph for this clause.</p>
        <label class="admin-field compact">
          <span>Redline Action</span>
          <select name="fallback_redline_action" id="dynamicRedlineAction">${options}</select>
        </label>
        <label class="admin-field ${wordingNeeded ? "" : "is-hidden"}" data-dynamic-wording="1">
          <span>Redline Wording${wordingNeeded ? " (required for replace / insert)" : ""}</span>
          <textarea name="fallback_wording" rows="3" placeholder="The approved language to substitute or insert.">${escapeHtml(wording)}</textarea>
        </label>
      </section>
    `;
  }

  // The structured decision conditions (pass / fail / review) the lint validates but
  // the UI previously could not author. Each condition is fully editable: its
  // description (what the AI judges), its issue_type, and -- for fail / review -- the
  // redline_action. The decision is fixed by which list the condition lives in
  // (pass->pass, fail->fail, review->review); the publish lint enforces that and
  // rejects malformed / contradictory conditions.
  const ISSUE_TYPES_BY_LIST = {
    pass_conditions: ["none"],
    fail_conditions: ["present_but_wrong", "missing", "unclear"],
    review_triggers: ["unclear"],
  };

  function dynamicDecisionControls(clause) {
    const rules = clause.rules && typeof clause.rules === "object" ? clause.rules : {};
    // governing_law and term_and_survival re-derive a couple of named condition
    // DESCRIPTIONS from their live levers on packet build, so those specific texts
    // can read back differently. The structure (which conditions exist, their
    // decision/issue_type/redline_action, and adding/removing conditions) is fully
    // live for every clause; flag the derived-description nuance for the author.
    const derivedNote = DERIVED_STANDARD_CLAUSES[clause.id]
      ? `<p class="admin-muted" data-derived-condition-note="1">Note: some condition descriptions for this clause are regenerated from ${escapeHtml(DERIVED_STANDARD_CLAUSES[clause.id])} on each review. Adding, removing, and re-typing conditions, issue types, and redline actions is still live.</p>`
      : "";
    return `
      <section class="admin-special" data-dynamic-conditions="1">
        <h3>Decision Conditions</h3>
        <p class="admin-muted">The structured pass / fail / review logic the AI applies. A clause needs at least one pass condition and at least one fail or review condition.</p>
        ${derivedNote}
        ${dynamicConditionGroup(clause, "pass_conditions", "Pass", rules.pass_conditions)}
        ${dynamicConditionGroup(clause, "fail_conditions", "Fail", rules.fail_conditions)}
        ${dynamicConditionGroup(clause, "review_triggers", "Review", rules.review_triggers)}
      </section>
    `;
  }

  function dynamicConditionGroup(clause, field, label, conditions) {
    const list = Array.isArray(conditions) ? conditions : [];
    const showRedline = field !== "pass_conditions";
    const rows = list
      .map((condition, index) => {
        const issueOptions = ISSUE_TYPES_BY_LIST[field]
          .map((value) => `<option value="${escapeHtml(value)}" ${String(condition.issue_type || "") === value ? "selected" : ""}>${escapeHtml(value)}</option>`)
          .join("");
        const redlineOptions = DYNAMIC_REDLINE_ACTIONS
          .map((opt) => `<option value="${escapeHtml(opt.value)}" ${String(condition.redline_action || "no_change") === opt.value ? "selected" : ""}>${escapeHtml(opt.label)}</option>`)
          .join("");
        return `
          <div class="admin-condition" data-condition-field="${escapeHtml(field)}" data-condition-index="${index}">
            <div class="admin-condition-head">
              <input type="text" data-condition-id="1" value="${escapeHtml(condition.id || "")}" placeholder="condition id (e.g. ${escapeHtml(field)}_1)">
              <button class="admin-chip removable" type="button" data-remove-condition="1" title="Remove condition"><span aria-hidden="true">x</span></button>
            </div>
            <textarea data-condition-description="1" rows="2" placeholder="What the AI should judge for this ${escapeHtml(label.toLowerCase())} outcome.">${escapeHtml(condition.description || "")}</textarea>
            <div class="admin-condition-meta">
              <label class="admin-field compact"><span>Issue type</span>
                <select data-condition-issue="1">${issueOptions}</select>
              </label>
              ${showRedline ? `<label class="admin-field compact"><span>Redline action</span><select data-condition-redline="1">${redlineOptions}</select></label>` : ""}
            </div>
          </div>
        `;
      })
      .join("");
    return `
      <div class="admin-condition-group" data-condition-group="${escapeHtml(field)}">
        <div class="admin-condition-group-head">
          <h4>${escapeHtml(label)} conditions</h4>
          <button class="secondary" type="button" data-add-condition="${escapeHtml(field)}">+ Add ${escapeHtml(label.toLowerCase())} condition</button>
        </div>
        ${rows || `<p class="admin-muted">No ${escapeHtml(label.toLowerCase())} conditions yet.</p>`}
      </div>
    `;
  }

  function termSurvivalPolicyControls(clause) {
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

  function termSurvivalDecisionControls() {
    return `
      <section class="admin-special">
        <h3>Checker Logic Visibility</h3>
        <p class="admin-muted">The backend evaluates survival language with document structure, explicit references, and deterministic concepts.</p>
        <dl class="admin-logic-list">
          <div><dt>Duration parser</dt><dd>Reads numeric and mixed word durations such as three (3) years and 3 (three) years.</dd></div>
          <div><dt>Reference resolver</dt><dd>When survival points to clauses or articles, the checker resolves those targets before deciding pass or check.</dd></div>
          <div><dt>Concept classifier</dt><dd>Referenced targets are tagged for confidentiality, use restriction, permitted disclosure, return/destruction, and carve-out concepts.</dd></div>
          <div><dt>Checker output</dt><dd>When references are used, the review result includes term_survival_analysis for audit.</dd></div>
        </dl>
      </section>
    `;
  }

  function governingLawForumForLaw(clause, law) {
    const options = clause.rules && Array.isArray(clause.rules.approved_options)
      ? clause.rules.approved_options
      : [];
    const id = optionIdForLaw(law);
    const match = options.find((option) => option && String(option.id || "") === id);
    return match && typeof match.forum_jurisdiction === "string" ? match.forum_jurisdiction : "";
  }

  function governingLawPolicyControls(clause) {
    const approved = clause.approved_laws || [];
    const preferredLaw = clause.preferred_law || approved[0] || "";
    const lawPhrases = clause.law_phrases || {};
    const rows = approved
      .map((law, index) => `
        <article class="admin-policy-option" data-governing-law-row="${index}">
          <label class="admin-policy-default">
            <input type="radio" name="preferred_law_index" value="${index}" data-preferred-governing-law="true" ${law === preferredLaw ? "checked" : ""}>
            <span>Preferred</span>
          </label>
          <label class="admin-field">
            <span>Jurisdiction</span>
            <input name="governing_law_value_${index}" data-governing-law-value="${index}" type="text" value="${escapeHtml(law)}">
          </label>
          <label class="admin-field">
            <span>Draft phrase</span>
            <input name="governing_law_phrase_${index}" data-governing-law-phrase="${index}" type="text" value="${escapeHtml(lawPhrases[law] || law)}">
          </label>
          <label class="admin-field">
            <span>Court / forum</span>
            <input name="governing_law_forum_${index}" data-governing-law-forum="${index}" type="text" value="${escapeHtml(governingLawForumForLaw(clause, law))}" placeholder="e.g. courts of England and Wales">
          </label>
          <button class="secondary admin-remove-button" type="button" data-remove-governing-law="${index}" ${approved.length <= 1 ? "disabled" : ""}>Remove</button>
        </article>
      `)
      .join("");
    return `
      <section class="admin-special">
        <h3>Approved Governing Laws</h3>
        <p class="admin-muted">These jurisdictions drive the AI assessment options, deterministic approved-law check, the law&lt;-&gt;forum recognition the review engine uses, and insertable Governing Law redline choices. The court / forum names the venue that must pair with each law and is required to publish.</p>
        <div class="admin-policy-options">${rows}</div>
        <div class="admin-inline-add">
          <input id="governingLawInput" type="text" placeholder="Add approved jurisdiction">
          <input id="governingLawForumInput" type="text" placeholder="Court / forum (e.g. courts of Singapore)">
          <button class="secondary" id="addGoverningLaw" type="button">Add</button>
        </div>
      </section>
    `;
  }

  function governingLawRedlineControls(clause) {
    return `
      <section class="admin-special">
        <h3>Generated Governing Law Redlines</h3>
        <p class="admin-muted">These options are generated from approved jurisdictions and draft phrases. Governing Law does not use a free redline template.</p>
        <div class="admin-generated-redlines" data-governing-law-redline-preview>${governingLawRedlinePreviewRows(clause)}</div>
      </section>
    `;
  }

  function redlineTemplateEditors(clause) {
    return templateConfigsForClause(clause)
      .filter((config) => config.field === "redline_template")
      .map((config) => templateEditorBlock(clause, config))
      .join("");
  }

  function templateEditorBlock(clause, config) {
    const value = String(clause[config.field] || "");
    const validation = validateTemplateValue(value, config);
    const preview = previewTemplateValue(value, clause);
    const placeholderCopy = config.placeholders.length
      ? config.placeholders.map((placeholder) => `<span class="admin-chip">{${escapeHtml(placeholder)}}</span>`).join("")
      : '<span class="admin-muted">No placeholders supported for this template.</span>';
    return `
      <section class="admin-template-field ${validation.error ? "invalid" : ""}" data-template-field="${escapeHtml(config.field)}">
        ${textArea(config.label, config.field, value, config.rows || 4)}
        <div class="admin-template-meta">
          <div>
            <h3>Template Preview</h3>
            <p data-template-preview="${escapeHtml(config.field)}">${escapeHtml(preview || "Preview appears after template text is entered.")}</p>
          </div>
          <div>
            <h3>Allowed Placeholders</h3>
            <div class="admin-chip-row">${placeholderCopy}</div>
          </div>
        </div>
        <p class="admin-template-error" data-template-validation="${escapeHtml(config.field)}">${escapeHtml(validation.error || "")}</p>
      </section>
    `;
  }

  function templateConfigsForClause(clause) {
    if (!clause) return [];
    const configs = {
      mutuality: [basicRedlineTemplateConfig()],
      confidential_information: [basicRedlineTemplateConfig(), standardExclusionsTemplateConfig()],
      term_and_survival: [{
        field: "redline_template",
        label: "Suggested Redline / Counter-language",
        placeholders: ["max_term_years", "max_term_years_label"],
        rows: 4,
      }],
      signatures: [basicRedlineTemplateConfig()],
    };
    return configs[clause.id] || [];
  }

  function basicRedlineTemplateConfig() {
    return {
      field: "redline_template",
      label: "Suggested Redline / Counter-language",
      placeholders: [],
      rows: 4,
    };
  }

  function standardExclusionsTemplateConfig() {
    return {
      field: "standard_exclusions_template",
      label: "Standard Exclusions Language",
      placeholders: [],
      rows: 3,
    };
  }

  function removeUnsupportedTemplateFields(clause) {
    const supportedFields = new Set(templateConfigsForClause(clause).map((config) => config.field));
    ["redline_template", "standard_exclusions_template"].forEach((field) => {
      if (!supportedFields.has(field)) delete clause[field];
    });
  }

  function sanitizePlaybookTemplatesForSave() {
    state.playbookClauses.forEach((clause) => {
      removeUnsupportedTemplateFields(clause);
      // _forumByOptionId is a transient FE-only scratch field used to thread the
      // authored court/forum into syncGoverningLawRules; it must never reach the
      // backend (it is not a valid clause field and would fail the publish gate).
      if (clause && Object.prototype.hasOwnProperty.call(clause, "_forumByOptionId")) {
        delete clause._forumByOptionId;
      }
    });
  }

  function validateTemplateValue(value, config) {
    const template = String(value || "").trim();
    if (!template) return { error: "Template cannot be blank." };
    const parsed = parseTemplatePlaceholders(template);
    if (parsed.invalid) {
      return { error: "Template has invalid placeholder syntax." };
    }
    const allowed = new Set(config.placeholders || []);
    const unknown = [...new Set(parsed.placeholders.filter((placeholder) => !allowed.has(placeholder)))].sort();
    if (unknown.length) {
      return { error: `Unknown placeholder${unknown.length === 1 ? "" : "s"}: ${unknown.join(", ")}.` };
    }
    return { error: "" };
  }

  function parseTemplatePlaceholders(template) {
    const placeholders = [];
    for (let index = 0; index < template.length; index += 1) {
      const character = template[index];
      if (character === "{" && template[index + 1] === "{") {
        index += 1;
        continue;
      }
      if (character === "}" && template[index + 1] === "}") {
        index += 1;
        continue;
      }
      if (character === "}") return { placeholders, invalid: true };
      if (character !== "{") continue;
      const end = template.indexOf("}", index + 1);
      if (end === -1) return { placeholders, invalid: true };
      const name = template.slice(index + 1, end).trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
        return { placeholders, invalid: true };
      }
      placeholders.push(name);
      index = end;
    }
    return { placeholders, invalid: false };
  }

  function previewTemplateValue(value, clause) {
    const template = String(value || "").trim();
    if (!template) return "";
    const context = templatePreviewContext(clause);
    return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_match, placeholder) => (
      Object.prototype.hasOwnProperty.call(context, placeholder)
        ? String(context[placeholder])
        : `{${placeholder}}`
    ));
  }

  function templatePreviewContext(clause) {
    if (clause?.id !== "term_and_survival") return {};
    const maxYears = Math.max(1, Number.parseInt(clause.max_term_years, 10) || TEMPLATE_PREVIEW_CONTEXT.max_term_years);
    return {
      max_term_years: maxYears,
      max_term_years_label: yearCountLabel(maxYears),
    };
  }

  function yearCountLabel(value) {
    const labels = {
      1: "one year",
      2: "two years",
      3: "three years",
      4: "four years",
      5: "five years",
    };
    return labels[value] || `${value} years`;
  }

  function renderTemplatePreviewState(clause) {
    templateConfigsForClause(clause).forEach((config) => {
      const value = String(clause[config.field] || "");
      const preview = clauseDetail.querySelector(`[data-template-preview="${config.field}"]`);
      const validation = clauseDetail.querySelector(`[data-template-validation="${config.field}"]`);
      const wrapper = clauseDetail.querySelector(`[data-template-field="${config.field}"]`);
      const result = validateTemplateValue(value, config);
      if (preview) preview.textContent = previewTemplateValue(value, clause) || "Preview appears after template text is entered.";
      if (validation) validation.textContent = result.error;
      if (wrapper) wrapper.classList.toggle("invalid", Boolean(result.error));
    });
  }

  function hasTemplateValidationErrors() {
    return state.playbookClauses.some((clause) => (
      templateConfigsForClause(clause).some((config) => validateTemplateValue(clause[config.field], config).error)
    ));
  }

  function templateValidationSummary() {
    for (const clause of state.playbookClauses) {
      for (const config of templateConfigsForClause(clause)) {
        const error = validateTemplateValue(clause[config.field], config).error;
        if (error) return `${clause.name || clause.id}: ${error}`;
      }
    }
    return "";
  }

  function governingLawRedlinePreviewRows(clause) {
    const approved = clause.approved_laws || [];
    const preferredLaw = clause.preferred_law || approved[0] || "";
    const lawPhrases = clause.law_phrases || {};
    return approved
      .map((law) => {
        const phrase = String(lawPhrases[law] || law).trim() || law;
        return `
          <article class="admin-redline-option-preview ${law === preferredLaw ? "preferred" : ""}">
            <strong>${escapeHtml(law)}${law === preferredLaw ? " <span>Preferred</span>" : ""}</strong>
            <p>${escapeHtml(governingLawTemplateText(phrase))}</p>
          </article>
        `;
      })
      .join("") || '<p class="admin-muted">Add an approved jurisdiction to generate redline options.</p>';
  }

  function renderGoverningLawRedlinePreview(clause) {
    if (clause?.id !== "governing_law") return;
    const preview = clauseDetail.querySelector("[data-governing-law-redline-preview]");
    if (preview) preview.innerHTML = governingLawRedlinePreviewRows(clause);
  }

  function governingLawTemplateText(phrase) {
    return `This Agreement shall be governed by the laws of ${phrase}.`;
  }

  function playbookHistoryPanel() {
    const history = Array.isArray(state.playbookHistory) ? state.playbookHistory.slice(0, 8) : [];
    const restoreDisabled = hasAnyDraft();
    const rows = history
      .map((entry) => {
        const changed = Array.isArray(entry.changed_clause_ids) && entry.changed_clause_ids.length
          ? entry.changed_clause_ids.join(", ")
          : "No clause-level changes";
        return `
          <article class="admin-history-row">
            <div>
              <strong>${escapeHtml(historyActionLabel(entry.action))}</strong>
              <span>${escapeHtml(formatHistoryDate(entry.recorded_at))} by ${escapeHtml(entry.actor || "admin")}</span>
              <p>${escapeHtml(entry.summary || changed)}</p>
              <small>${escapeHtml(changed)}</small>
            </div>
            <button class="secondary" type="button" data-restore-playbook-version="${escapeHtml(entry.id || "")}" ${restoreDisabled || !entry.id ? "disabled" : ""}>Restore</button>
          </article>
        `;
      })
      .join("");
    return `
      <section class="admin-special admin-history">
        <h3>Policy Version History</h3>
        <p class="admin-muted">Every published Playbook stores a restorable snapshot. Restore loads a version into the draft and is disabled while there are unsaved changes.</p>
        ${rows || '<p class="admin-muted">No published policy versions yet.</p>'}
      </section>
    `;
  }

  function setupPlaybookHistoryControls() {
    clauseDetail.querySelectorAll("[data-restore-playbook-version]").forEach((button) => {
      button.addEventListener("click", () => {
        const historyId = button.dataset.restorePlaybookVersion;
        if (historyId) restorePlaybookVersion(historyId);
      });
    });
  }

  function historyActionLabel(action) {
    if (action === "baseline") return "Baseline";
    if (action === "restore") return "Restored";
    if (action === "publish") return "Published";
    return "Saved";
  }

  function formatHistoryDate(value) {
    if (!value) return "Unknown time";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, {
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
    });
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
    const outputRows = [
      ["Checker module", visibility.module],
      ["Analysis purpose", visibility.purpose],
      ["Primary inputs", visibility.inputs],
      ["Audit output", visibility.output_field],
      ["Review state", "Every checker emits review_state to normalize pass, review, and check routing, send blocking, and redline requirements."],
      ["Reason codes", "Every checker emits reason_code and reason_codes so audit, admin views, and AI handoff can classify the decision without parsing prose."],
      ["Structured evidence", "Every checker emits structured_evidence records with paragraph provenance, matched terms, signal type, rule bucket, counted flag, and reason."],
      ["AI semantic review", "When NDA_AI_REVIEW_ENABLED is set, AI runs as a blind second opinion: it never receives the Python decision, reason, or checker analysis and independently returns pass/review/fail from the playbook requirement and document paragraphs. The backend then compares the two, and ai_review_analysis records the AI decision, confidence, cited spans, validation status, and any deterministic/AI disagreement."],
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
    const analysisFieldNames = [...visibility.analysis_fields, "ai_review_analysis"];

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
          <div class="admin-chip-row">${analysisFieldNames.map((field) => `<span class="admin-chip">${escapeHtml(field)}</span>`).join("") || '<span class="admin-muted">No checker-specific analysis object yet</span>'}</div>
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
      optional_ai_review_field: "ai_review_analysis",
      signal_buckets: visibility.signal_buckets,
      reason_code_taxonomy: visibility.reason_codes || {},
      hardening_guards: visibility.hardening_guards || [],
      audit_reading_order: [
        "review_state",
        "reason_code",
        "reason_codes",
        "structured_evidence",
        visibility.output_field,
        "ai_review_analysis",
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
          "conditional_override_paragraph_ids",
          "candidate_records",
        ],
        reason_codes: {
          pass: ["approved_governing_law"],
          review: ["unclear_governing_law", "governing_law_heading_only", "governing_law_conditional_override"],
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
        module: "AI-first Playbook guidance",
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

  function applyGoverningLawFormData(clause, form, data) {
    const rows = [...form.querySelectorAll("[data-governing-law-row]")];
    const approvedLaws = [];
    const lawPhrases = {};
    // Authored court/forum per option, keyed by the option's derived id, so
    // syncGoverningLawRules can write it onto the matching merged option object.
    const forumByOptionId = {};
    rows.forEach((row) => {
      const law = String(row.querySelector("[data-governing-law-value]")?.value || "").trim();
      if (!law) return;
      if (approvedLaws.some((item) => item.toLowerCase() === law.toLowerCase())) return;
      const phrase = String(row.querySelector("[data-governing-law-phrase]")?.value || "").trim() || law;
      const forum = String(row.querySelector("[data-governing-law-forum]")?.value || "").trim();
      approvedLaws.push(law);
      lawPhrases[law] = phrase;
      forumByOptionId[optionIdForLaw(law)] = forum;
    });
    clause.approved_laws = approvedLaws;
    clause.law_phrases = lawPhrases;
    clause._forumByOptionId = forumByOptionId;
    const preferredIndex = Number.parseInt(data.get("preferred_law_index"), 10);
    clause.preferred_law = approvedLaws[preferredIndex] || approvedLaws[0] || "";
  }

  function syncStructuredRules(clause, changedField) {
    if (!clause.rules || typeof clause.rules !== "object") return;
    clause.rules.clause_type = clause.type;
    if (changedField === "preferred_position" && clause.preferred_position) {
      clause.rules.acceptable_position = clause.preferred_position;
    }
    if (isDynamicClause(clause)) {
      // Keep the structured rules.acceptable_position coherent with the authored
      // acceptable language / preferred position so the lint's referential checks
      // and the binding-policy block read a non-empty, consistent value.
      const acceptable = String(clause.acceptable_language || clause.preferred_position || "").trim();
      if (acceptable) {
        clause.rules.acceptable_position = acceptable;
      }
    }
    if (clause.id === "governing_law") {
      syncGoverningLawRules(clause);
    }
  }

  function syncGoverningLawRules(clause) {
    const approved = dedupeList(clause.approved_laws || []);
    clause.approved_laws = approved;
    if (!approved.includes(clause.preferred_law)) {
      clause.preferred_law = approved[0] || "";
    }
    const lawPhrases = {};
    const existingPhrases = clause.law_phrases || {};
    approved.forEach((law) => {
      lawPhrases[law] = String(existingPhrases[law] || law).trim() || law;
    });
    clause.law_phrases = lawPhrases;
    const rules = clause.rules || {};
    // Rebuild the option list by MERGING the {id,label,value,default} the editor
    // controls onto the EXISTING loaded option objects (matched by derived id),
    // never replacing them outright. The backend authors per-option fields the
    // FE has no control for -- forum_jurisdiction (the law<->court pairing the AI
    // forum check + generation read), aliases, entity_prefixes (the governing-law
    // checker's recognition terms). Replacing the array would strip those before
    // the POST, and the backend carry-over could not recover what never arrived.
    const existingOptions = Array.isArray(rules.approved_options) ? rules.approved_options : [];
    const existingById = {};
    existingOptions.forEach((option) => {
      if (!option || typeof option !== "object") return;
      const id = String(option.id || optionIdForLaw(option.value || option.label || "")).trim();
      if (id) existingById[id] = option;
    });
    const forumByOptionId = (clause._forumByOptionId && typeof clause._forumByOptionId === "object")
      ? clause._forumByOptionId
      : {};
    rules.approved_options = approved.map((law) => {
      const id = optionIdForLaw(law);
      const prior = existingById[id];
      const merged = (prior && typeof prior === "object") ? { ...prior } : {};
      merged.id = id;
      merged.label = law;
      merged.value = law;
      merged.default = law === clause.preferred_law;
      // The authored court/forum: take the edited value when this sync ran from a
      // forum edit, otherwise preserve whatever the merged option already carried.
      if (Object.prototype.hasOwnProperty.call(forumByOptionId, id)) {
        const forum = String(forumByOptionId[id] || "").trim();
        if (forum) merged.forum_jurisdiction = forum;
        else delete merged.forum_jurisdiction;
      }
      return merged;
    });
    if (rules.redline_guidance && typeof rules.redline_guidance === "object") {
      const preferred = clause.preferred_law || approved[0] || "the preferred approved jurisdiction";
      rules.redline_guidance.drafting_note = `Use one of the approved jurisdiction options. Default to ${preferred} unless another approved option is selected.`;
    }
    clause.rules = rules;
  }

  function stanceLabel(clause) {
    return clause.type === "prohibited" ? "Prohibited" : "Required";
  }

  function valueAt(object, path) {
    return path.split(".").reduce((value, key) => {
      if (!value || typeof value !== "object") return undefined;
      return value[key];
    }, object);
  }

  function stableJson(value) {
    return JSON.stringify(value === undefined ? null : value);
  }

  function formatDiffValue(value) {
    if (Array.isArray(value)) return `[${value.join(", ")}]`;
    if (value && typeof value === "object") return JSON.stringify(value, null, 2);
    if (typeof value === "boolean") return value ? "true" : "false";
    if (value === undefined || value === null || value === "") return "(blank)";
    return String(value);
  }

  function clonePlaybook(value) {
    return JSON.parse(JSON.stringify(value || {}));
  }

  function dedupeList(values) {
    const seen = new Set();
    return values.map((value) => String(value).trim()).filter((value) => {
      const key = String(value).trim();
      const normalized = key.toLowerCase();
      if (!key || seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    });
  }

  function optionIdForLaw(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  return { loadPlaybook, renderClauseDetail, renderPlaybookList };
}
