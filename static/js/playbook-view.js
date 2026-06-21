function createPlaybookController({ state, playbookList, clauseDetail, renderStudioEmpty, runtime = (typeof PlaybookRuntime !== "undefined" ? PlaybookRuntime : null) }) {
  const TEMPLATE_PREVIEW_CONTEXT = {
    max_term_years: 5,
  };

  // DISPLAY-ONLY label maps. These translate machine field/issue keys into the
  // friendly wording the author already sees elsewhere in this editor. They are
  // never written back into a value, key, or data-attribute -- the raw key always
  // remains the stored/posted value. Unknown keys fall back to window.humanizeId.
  const FIELD_LABELS = {
    requirement: "Requirement",
    preferred_position: "Preferred Standard Position",
    acceptable_language: "Approved Language",
    redline_template: "Suggested Redline",
    check_trigger: "Check Trigger Position",
    standard_exclusions_template: "Standard Exclusions Language",
  };
  function fieldLabel(field) {
    const key = String(field == null ? "" : field);
    if (Object.prototype.hasOwnProperty.call(FIELD_LABELS, key)) return FIELD_LABELS[key];
    const fallback = typeof window !== "undefined" && typeof window.humanizeId === "function"
      ? window.humanizeId
      : (value) => String(value || "");
    return fallback(key);
  }

  // Friendly label for a (possibly dotted) draft-diff field path, e.g.
  // "rules.pass_conditions" -> "Pass Conditions". The leaf segment is run through
  // the same curated label dict / humanizer; display-only.
  function diffFieldLabel(field) {
    const path = String(field == null ? "" : field);
    const leaf = path.includes(".") ? path.slice(path.lastIndexOf(".") + 1) : path;
    return fieldLabel(leaf);
  }

  const ISSUE_TYPE_LABELS = {
    none: "No issue",
    present_but_wrong: "Present but non-compliant",
    missing: "Missing / absent",
    unclear: "Ambiguous — needs review",
  };
  function issueTypeLabel(issueType) {
    const key = String(issueType == null ? "" : issueType);
    if (Object.prototype.hasOwnProperty.call(ISSUE_TYPE_LABELS, key)) return ISSUE_TYPE_LABELS[key];
    const fallback = typeof window !== "undefined" && typeof window.humanizeId === "function"
      ? window.humanizeId
      : (value) => String(value || "");
    return fallback(key);
  }

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
      renderPlaybookLevelHistory();
      return;
    }
    renderConsolidatedClauseDetail(clause);
    renderPlaybookLevelHistory();
  }

  // ONE consolidated, scrolling clause view -- no per-clause sub-tabs. Stance, the
  // check-driving lists, the policy prose, and the redline templates are all
  // visible together so the author can see how the lists feed the prose and the
  // redline. The Decision Logic tab (the read-only checker-internals dump) is gone;
  // only two tiny salvaged notes survive (governing-law blind-second-opinion and
  // non-circumvention AI-adjudicated). The Audit content is trimmed to the live
  // draft diff (inline at the bottom); Version History/Restore is GLOBAL and lives
  // at the playbook level (#playbookHistory), rendered separately.
  function renderConsolidatedClauseDetail(clause) {
    clauseDetail.innerHTML = `
      <form class="playbook-editor playbook-editor-consolidated" id="playbookEditor">
        <div class="admin-head">
          <div>
            <p class="eyebrow" title="Playbook clause id: ${escapeHtml(clause.id)}">Playbook clause</p>
            <h2>Edit Clause: ${escapeHtml(clause.name)}</h2>
          </div>
          <span class="policy-chip ${escapeHtml(clause.type)}">${escapeHtml(stanceLabel(clause))}</span>
        </div>

        ${playbookStatusBanner()}

        <section class="playbook-clause-section" data-clause-section="policy">
          <div class="playbook-subpanel-head">
            <h3>Policy</h3>
            <p>Define the ${escapeHtml(clause.name)} rule the AI review should apply.</p>
          </div>
          ${clauseAdjudicationNote(clause)}
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
          ${checkDrivingListControls(clause)}
          ${triggerTermsControls(clause)}
          ${dynamicDecisionControls(clause)}
        </section>

        <section class="playbook-clause-section" data-clause-section="redline">
          <div class="playbook-subpanel-head">
            <h3>Redline</h3>
            <p>Control the language exported when ${escapeHtml(clause.name)} needs a redline.</p>
          </div>
          ${redlinePanelControls(clause)}
        </section>

        <section class="playbook-clause-section playbook-clause-diff" data-clause-section="diff">
          <div class="playbook-subpanel-head">
            <h3>Unsaved changes (this clause)</h3>
            <p>The live diff of your edits to ${escapeHtml(clause.name)} against the last saved draft.</p>
          </div>
          <section class="admin-rules diff">
            <pre id="playbookDraftDiff">${escapeHtml(diffForClause(clause.id) || "No unsaved changes.")}</pre>
          </section>
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
    setupCheckDrivingListControls(clause);
    setupAiWordingControls(clause);
  }

  // Salvaged one-liner notes from the deleted Decision Logic tab. governing_law:
  // AI runs as a blind second opinion. non_circumvention: AI-adjudicated rather
  // than a rule-based checker.
  function clauseAdjudicationNote(clause) {
    if (clause.id === "governing_law") {
      return '<p class="admin-note playbook-adjudication-note" data-adjudication-note="governing_law">AI gives a blind second opinion: it independently re-checks governing law and the backend compares the two verdicts.</p>';
    }
    if (clause.id === "non_circumvention") {
      return '<p class="admin-note playbook-adjudication-note" data-adjudication-note="non_circumvention">AI-adjudicated: the AI judges this clause from the requirement and prohibited positions below.</p>';
    }
    return "";
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
            ? `<span class="playbook-validation-where">${escapeHtml(clauseNameForId(error.clause_id))}${error.field ? ` &middot; ${escapeHtml(fieldLabel(error.field))}` : ""}</span>`
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
          ? `<span class="playbook-validation-where">${escapeHtml(clauseNameForId(error.clause_id))}${error.field ? ` &middot; ${escapeHtml(fieldLabel(error.field))}` : ""}</span>`
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
      // Check-driving lists now editable via the add/remove editors. Registering
      // them here is what makes an edit mark the draft dirty so Save enables.
      "indefinite_terms",
      "one_way_terms",
      "definition_categories",
      "problematic_exclusion_terms",
      "prohibited_position_patterns",
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
      .map((field) => `${diffFieldLabel(field)}:\n- ${formatDiffValue(valueAt(saved, field))}\n+ ${formatDiffValue(valueAt(clause, field))}`)
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
        ${aiDraftedHint(clause, "requirement")}
        ${textArea("Acceptable / Approved Language", "acceptable_language", String(clause.acceptable_language || ""), 3)}
        ${aiDraftedHint(clause, "acceptable_language")}
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
  // Hardcoded fallback so this branch works STANDALONE before the backend marker
  // lands: {clause_id: [derived field names]}.
  const FALLBACK_DERIVED_FIELDS = {
    governing_law: ["preferred_position", "check_trigger"],
    term_and_survival: ["preferred_position", "check_trigger"],
  };

  // Which policy fields are server-derived (edits discarded) for this clause.
  // Prefers the backend-provided marker -- a parallel agent is adding either a
  // clause.derived_policy_fields list or a clause.derived:true marker. Falls back
  // to the hardcoded set so the editor greys the right boxes even standalone.
  function derivedFieldsForClause(clause) {
    if (!clause) return new Set();
    if (Array.isArray(clause.derived_policy_fields)) {
      return new Set(clause.derived_policy_fields.map((field) => String(field)));
    }
    if (clause.derived === true) {
      return new Set(FALLBACK_DERIVED_FIELDS[clause.id] || ["preferred_position", "check_trigger"]);
    }
    return new Set(FALLBACK_DERIVED_FIELDS[clause.id] || []);
  }

  // Inline "AI-drafted -- review before publishing" hint, shown only for a field
  // whose current text came from an applied AI suggestion (tracked transiently on
  // clause._aiDraftedFields). Cleared implicitly when the clause is reloaded/saved.
  function aiDraftedHint(clause, field) {
    const drafted = clause && clause._aiDraftedFields && clause._aiDraftedFields[field];
    if (!drafted) return "";
    return `<p class="admin-note playbook-ai-drafted-hint" data-ai-drafted="${escapeHtml(field)}">AI-drafted -- review before publishing.</p>`;
  }

  function standardPositionControls(clause) {
    const derived = derivedFieldsForClause(clause);
    const lever = DERIVED_STANDARD_CLAUSES[clause.id];
    if (derived.has("preferred_position") || derived.has("check_trigger")) {
      const leverNote = lever
        ? `For this clause the preferred position and check trigger are generated from ${escapeHtml(lever)} on every review. They cannot be edited as free text here -- change the live lever instead.`
        : "These fields are auto-derived by the AI review. Edit the live lever to change them.";
      return `
        <section class="admin-special" data-derived-standard="1">
          <h3>Standard Position (derived)</h3>
          <p class="admin-muted">${leverNote}</p>
          <label class="admin-field compact"><span>Preferred Standard Position (read-only, derived)</span>
            <textarea rows="3" readonly disabled data-derived-field="preferred_position">${escapeHtml(preferredPosition(clause))}</textarea>
            <small class="admin-muted">Auto-derived from the approved list -- edit the list to change this.</small>
          </label>
          <label class="admin-field compact"><span>Check Trigger Position (read-only, derived)</span>
            <textarea rows="3" readonly disabled data-derived-field="check_trigger">${escapeHtml(checkTrigger(clause))}</textarea>
            <small class="admin-muted">Auto-derived from the approved list -- edit the list to change this.</small>
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
      ${aiDraftedHint(clause, "preferred_position")}
      ${textArea("Check Trigger Position", "check_trigger", checkTrigger(clause), 3)}
      ${aiDraftedHint(clause, "check_trigger")}
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
        <h3>Trigger Keywords</h3>
        <p class="admin-muted">Words and phrases that flag this clause for review (used by both the automatic check and the AI). At least one is required.</p>
        <label class="admin-field compact"><span>Trigger Keywords</span></label>
        <div class="admin-chip-row" data-chip-row="search-term">${searchTerms || '<span class="admin-muted">No trigger keywords yet</span>'}</div>
        <div class="admin-inline-add">
          <input type="text" id="dynamicSearchTermInput" placeholder="e.g. non-compete">
          <button class="secondary" type="button" id="addDynamicSearchTerm">Add</button>
        </div>
        <label class="admin-field compact"><span>AI Context Clues (optional)</span></label>
        <p class="admin-muted">Context hints that guide the AI's understanding of this clause (optional).</p>
        <div class="admin-chip-row" data-chip-row="semantic-signal">${semanticSignals || '<span class="admin-muted">No AI context clues yet</span>'}</div>
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

  // ---------------------------------------------------------------------------
  // Check-driving list editors (Task 4 + 5 + 6)
  // ---------------------------------------------------------------------------
  // The lists below directly drive the deterministic / AI check (not just the AI
  // packet like search_terms). Each used to render as read-only chips; now each is
  // a full add/remove editor reusing the chipList pattern. For non_circumvention
  // the list is {label, pattern}[] but the UI only ever exposes the NAME (label);
  // the regex pattern is auto-derived behind the scenes.
  //
  // CRITICAL: every field here is also registered in diffForClause's field list so
  // edits mark the draft dirty and Save enables.
  const CHECK_DRIVING_LISTS = {
    term_and_survival: [{
      field: "indefinite_terms",
      kind: "string",
      title: "Perpetual / Indefinite Trigger Terms",
      help: "When these terms appear outside the permitted carve-out context, the clause is checked.",
      placeholder: "e.g. in perpetuity",
      empty: "No indefinite trigger terms configured",
    }],
    mutuality: [{
      field: "one_way_terms",
      kind: "string",
      title: "One-Way / Unilateral Terms",
      help: "Operative one-way or recipient-only language that forces a mutuality check.",
      placeholder: "e.g. only the Receiving Party",
      empty: "No one-way terms configured",
    }],
    confidential_information: [
      {
        field: "definition_categories",
        kind: "string",
        title: "Required Definition Categories",
        help: "Categories the Confidential Information definition should cover. We flag the clause if the definition is too narrow.",
        placeholder: "e.g. technical information",
        empty: "No required categories configured",
      },
      {
        field: "problematic_exclusion_terms",
        kind: "string",
        title: "Problematic Exclusion Terms",
        help: "Extra carve-outs that weaken protection. Their presence flags the clause.",
        placeholder: "e.g. residual knowledge",
        empty: "No problematic exclusion terms configured",
      },
    ],
    non_circumvention: [{
      field: "prohibited_position_patterns",
      kind: "named_pattern",
      title: "Prohibited Position Names",
      help: "The kinds of clauses this position bans (by name). We work out how to detect each one automatically.",
      placeholder: "e.g. non_compete",
      empty: "No prohibited positions configured",
    }],
  };

  function checkDrivingListsForClause(clause) {
    return CHECK_DRIVING_LISTS[clause && clause.id] || [];
  }

  // Read the editable names from a check-driving list field. For named_pattern
  // lists ({label, pattern}[]) the UI value is the label; plain string lists are
  // returned as-is.
  function checkDrivingListValues(clause, config) {
    const raw = Array.isArray(clause[config.field]) ? clause[config.field] : [];
    if (config.kind === "named_pattern") {
      return raw
        .map((entry) => (entry && typeof entry === "object" ? String(entry.label || "") : String(entry || "")))
        .filter(Boolean);
    }
    return raw.map((item) => String(item || "")).filter(Boolean);
  }

  // Auto-derive a meaning-targeted regex from a prohibited-position NAME so the
  // backend pattern field stays populated without the admin ever seeing a regex.
  // Tokens are escaped (so the derived pattern always compiles) and joined with a
  // flexible whitespace/hyphen separator ("data localization" -> "data[\s-]+localization").
  function derivePatternFromName(name) {
    const tokens = String(name || "")
      .trim()
      .split(/[\s_-]+/)
      .filter(Boolean)
      .map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!tokens.length) return "";
    return tokens.join("[\\s-]+");
  }

  // Write the editable names back onto the clause field. For named_pattern lists we
  // MERGE onto existing entries (preserving any hand-authored pattern for a name
  // that already existed) and derive a pattern for newly-added names.
  function setCheckDrivingListValues(clause, config, names) {
    const deduped = dedupeList(names);
    if (config.kind !== "named_pattern") {
      clause[config.field] = deduped;
      return;
    }
    const existing = Array.isArray(clause[config.field]) ? clause[config.field] : [];
    const byLabel = {};
    existing.forEach((entry) => {
      if (entry && typeof entry === "object" && entry.label) {
        byLabel[String(entry.label).toLowerCase()] = entry;
      }
    });
    clause[config.field] = deduped.map((label) => {
      const prior = byLabel[label.toLowerCase()];
      if (prior && typeof prior === "object" && typeof prior.pattern === "string" && prior.pattern) {
        return { ...prior, label };
      }
      return { label, pattern: derivePatternFromName(label) };
    });
  }

  // Has the author edited any check-driving list for this clause WITHOUT updating
  // the dependent prose (requirement / preferred_position / redline_template)?
  // Drives the drift warning (Task 6).
  function checkDrivingListDrift(clause) {
    const saved = savedClause(clause.id);
    if (!saved) return false;
    const listChanged = checkDrivingListsForClause(clause).some(
      (config) => stableJson(valueAt(clause, config.field)) !== stableJson(valueAt(saved, config.field)),
    );
    if (!listChanged) return false;
    const proseFields = ["requirement", "preferred_position", "acceptable_language", "redline_template"];
    const proseChanged = proseFields.some(
      (field) => stableJson(valueAt(clause, field)) !== stableJson(valueAt(saved, field)),
    );
    return !proseChanged;
  }

  function checkDrivingListControls(clause) {
    const configs = checkDrivingListsForClause(clause);
    if (!configs.length) return "";
    const drift = checkDrivingListDrift(clause)
      ? `<p class="admin-note playbook-drift-warning" data-list-drift="1">Your redline / prose doesn't mention the new item yet. Use <strong>Update wording with AI</strong>, or edit the prose manually.</p>`
      : "";
    const blocks = configs
      .map((config) => {
        const names = checkDrivingListValues(clause, config);
        const chips = names
          .map((name) => `
            <button class="admin-chip removable" type="button" data-remove-list-item="${escapeHtml(config.field)}" data-list-value="${escapeHtml(name)}">
              ${escapeHtml(name)} <span aria-hidden="true">x</span>
            </button>
          `)
          .join("");
        const nameOnly = config.kind === "named_pattern"
          ? '<small class="admin-muted">Names only - detection is handled automatically.</small>'
          : "";
        return `
          <section class="admin-special playbook-check-list" data-check-list="${escapeHtml(config.field)}">
            <h3>${escapeHtml(config.title)}</h3>
            <p class="admin-muted">${escapeHtml(config.help)}</p>
            ${nameOnly}
            <div class="admin-chip-row" data-list-row="${escapeHtml(config.field)}">${chips || `<span class="admin-muted">${escapeHtml(config.empty)}</span>`}</div>
            <div class="admin-inline-add">
              <input type="text" data-list-input="${escapeHtml(config.field)}" placeholder="${escapeHtml(config.placeholder)}">
              <button class="secondary" type="button" data-list-add="${escapeHtml(config.field)}">Add</button>
            </div>
          </section>
        `;
      })
      .join("");
    return `
      <section class="playbook-check-lists" data-check-lists="1">
        ${drift}
        ${blocks}
        ${aiWordingControls(clause)}
      </section>
    `;
  }

  function setupCheckDrivingListControls(clause) {
    const configs = checkDrivingListsForClause(clause);
    if (!configs.length) return;
    const byField = {};
    configs.forEach((config) => { byField[config.field] = config; });

    const addFor = (field) => {
      const config = byField[field];
      const input = clauseDetail.querySelector(`[data-list-input="${field}"]`);
      if (!config || !input) return;
      const value = input.value.trim();
      if (!value) return;
      const next = [...checkDrivingListValues(clause, config), value];
      setCheckDrivingListValues(clause, config, next);
      input.value = "";
      renderClauseDetail();
    };

    clauseDetail.querySelectorAll("[data-list-add]").forEach((button) => {
      button.addEventListener("click", () => addFor(button.dataset.listAdd));
    });
    clauseDetail.querySelectorAll("[data-list-input]").forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        addFor(input.dataset.listInput);
      });
    });
    clauseDetail.querySelectorAll("[data-remove-list-item]").forEach((button) => {
      button.addEventListener("click", () => {
        const field = button.dataset.removeListItem;
        const config = byField[field];
        if (!config) return;
        const value = button.dataset.listValue;
        const next = checkDrivingListValues(clause, config).filter((item) => item !== value);
        setCheckDrivingListValues(clause, config, next);
        renderClauseDetail();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // "Update wording with AI" (Task 5)
  // ---------------------------------------------------------------------------
  // Lets the admin ask the AI to redraft the dependent prose so it matches the
  // edited lists. The button POSTs the clause + its prose fields; the response is
  // a per-field {old,new,changed} suggestion set. NOTHING changes until the admin
  // clicks Apply in the diff preview -- never auto-apply.
  const AI_WORDING_FIELDS = ["requirement", "preferred_position", "acceptable_language", "redline_template"];

  function aiWordingControls(clause) {
    return `
      <section class="admin-special playbook-ai-wording" data-ai-wording="1">
        <div class="playbook-ai-wording-head">
          <h3>Update wording with AI</h3>
          <button class="secondary" type="button" data-ai-wording-trigger="1">Update wording with AI</button>
        </div>
        <p class="admin-muted">Ask the AI to redraft the prose and redline so they match the lists above. You review a diff and click Apply before anything changes.</p>
        <div class="playbook-ai-wording-status" data-ai-wording-status="1" aria-live="polite"></div>
        <div class="playbook-ai-wording-diff" data-ai-wording-diff="1"></div>
      </section>
    `;
  }

  function aiWordingFieldsPayload(clause) {
    const fields = {};
    AI_WORDING_FIELDS.forEach((field) => {
      if (typeof clause[field] === "string") fields[field] = clause[field];
    });
    return fields;
  }

  function setupAiWordingControls(clause) {
    const trigger = clauseDetail.querySelector("[data-ai-wording-trigger]");
    if (!trigger) return;
    trigger.addEventListener("click", () => requestAiWording(clause));
  }

  async function requestAiWording(clause) {
    const status = clauseDetail.querySelector("[data-ai-wording-status]");
    const diffRegion = clauseDetail.querySelector("[data-ai-wording-diff]");
    const trigger = clauseDetail.querySelector("[data-ai-wording-trigger]");
    if (diffRegion) diffRegion.innerHTML = "";
    if (status) status.textContent = "Asking the AI to update the wording...";
    if (trigger) trigger.disabled = true;
    try {
      const payload = await postSuggestWording(clause.id, {
        clause,
        fields: aiWordingFieldsPayload(clause),
      });
      const suggestions = payload && typeof payload.suggestions === "object" ? payload.suggestions : {};
      const warnings = Array.isArray(payload?.warnings) ? payload.warnings : [];
      const validationOk = payload?.validation_ok !== false;
      const changed = Object.entries(suggestions).filter(([, value]) => value && value.changed);
      if (!changed.length) {
        if (status) status.textContent = "The AI did not suggest any wording changes.";
        return;
      }
      if (status) status.textContent = "Review the AI's suggested wording, then Apply per field.";
      renderAiWordingDiff(clause, suggestions, warnings, validationOk);
    } catch (error) {
      if (status) status.textContent = error.message || "The AI wording update is unavailable right now.";
    } finally {
      if (trigger) trigger.disabled = false;
    }
  }

  // POST to the suggest-wording endpoint. Built defensively: a parallel agent owns
  // the backend route; until it lands a 404 surfaces as a friendly "unavailable"
  // message rather than a crash.
  async function postSuggestWording(clauseId, body) {
    const api = playbookApi();
    if (api && typeof api.suggestWording === "function") {
      return api.suggestWording(clauseId, body);
    }
    const response = await fetch(`/api/playbook/clause/${encodeURIComponent(clauseId)}/suggest-wording`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || "The AI wording update is unavailable right now.");
    }
    return payload;
  }

  // Render the per-field old-vs-new diff with Apply / Edit / Cancel. Apply writes
  // the new text into the clause (then the normal Save/Publish persists it). When
  // validation_ok is false the warnings show and Apply is disabled per field.
  function renderAiWordingDiff(clause, suggestions, warnings, validationOk) {
    const diffRegion = clauseDetail.querySelector("[data-ai-wording-diff]");
    if (!diffRegion) return;
    const warningBlock = warnings.length
      ? `<div class="playbook-ai-wording-warnings" data-ai-wording-warnings="1"><strong>Warnings:</strong><ul>${warnings.map((w) => `<li>${escapeHtml(typeof w === "string" ? w : (w && w.message) || "")}</li>`).join("")}</ul></div>`
      : "";
    const cards = Object.entries(suggestions)
      .filter(([, value]) => value && value.changed)
      .map(([field, value]) => {
        const applyDisabled = validationOk ? "" : "disabled";
        return `
          <article class="playbook-ai-wording-card" data-ai-wording-field="${escapeHtml(field)}">
            <h4>${escapeHtml(fieldLabel(field))}</h4>
            <div class="playbook-ai-wording-cols">
              <div class="playbook-ai-wording-old"><span class="admin-muted">Current</span><pre>${escapeHtml(String(value.old || ""))}</pre></div>
              <div class="playbook-ai-wording-new"><span class="admin-muted">AI suggestion</span><textarea data-ai-wording-text="${escapeHtml(field)}" rows="4" readonly>${escapeHtml(String(value.new || ""))}</textarea></div>
            </div>
            <div class="playbook-ai-wording-actions">
              <button class="primary" type="button" data-ai-wording-apply="${escapeHtml(field)}" ${applyDisabled}>Apply</button>
              <button class="secondary" type="button" data-ai-wording-edit="${escapeHtml(field)}">Edit</button>
              <button class="secondary" type="button" data-ai-wording-cancel="${escapeHtml(field)}">Cancel</button>
            </div>
          </article>
        `;
      })
      .join("");
    diffRegion.innerHTML = `${warningBlock}<div class="playbook-ai-wording-cards">${cards}</div>`;

    diffRegion.querySelectorAll("[data-ai-wording-edit]").forEach((button) => {
      button.addEventListener("click", () => {
        const field = button.dataset.aiWordingEdit;
        const textarea = diffRegion.querySelector(`[data-ai-wording-text="${field}"]`);
        if (textarea) {
          textarea.readOnly = false;
          textarea.focus();
        }
      });
    });
    diffRegion.querySelectorAll("[data-ai-wording-cancel]").forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest("[data-ai-wording-field]");
        if (card) card.remove();
      });
    });
    diffRegion.querySelectorAll("[data-ai-wording-apply]").forEach((button) => {
      if (button.disabled) return;
      button.addEventListener("click", () => {
        const field = button.dataset.aiWordingApply;
        const textarea = diffRegion.querySelector(`[data-ai-wording-text="${field}"]`);
        const text = textarea ? String(textarea.value || "") : "";
        applyAiWording(clause, field, text);
      });
    });
  }

  // Apply ONE AI suggestion: write the new text into the clause, keep the rules
  // coherent, mark the field AI-drafted, and re-render so the diff + Save state
  // update. Only Apply mutates anything.
  function applyAiWording(clause, field, text) {
    clause[field] = text;
    if (field === "preferred_position") {
      syncStructuredRules(clause, "preferred_position");
    } else if (field === "acceptable_language" && isDynamicClause(clause)) {
      syncStructuredRules(clause);
    }
    if (!clause._aiDraftedFields || typeof clause._aiDraftedFields !== "object") {
      clause._aiDraftedFields = {};
    }
    clause._aiDraftedFields[field] = true;
    renderClauseDetail();
    const status = clauseDetail.querySelector("[data-ai-wording-status]");
    if (status) status.textContent = `Applied AI wording to ${fieldLabel(field)}. AI-drafted - review before publishing.`;
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
        <p class="admin-muted">This clause uses generated redline behavior from the AI review.</p>
      </section>
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
        <p class="admin-muted">What the AI review does to a flagged paragraph for this clause.</p>
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
          .map((value) => `<option value="${escapeHtml(value)}" ${String(condition.issue_type || "") === value ? "selected" : ""}>${escapeHtml(issueTypeLabel(value))}</option>`)
          .join("");
        const redlineOptions = DYNAMIC_REDLINE_ACTIONS
          .map((opt) => `<option value="${escapeHtml(opt.value)}" ${String(condition.redline_action || "no_change") === opt.value ? "selected" : ""}>${escapeHtml(opt.label)}</option>`)
          .join("");
        return `
          <div class="admin-condition" data-condition-field="${escapeHtml(field)}" data-condition-index="${index}">
            <div class="admin-condition-head">
              <input type="text" data-condition-id="1" value="${escapeHtml(condition.id || "")}" placeholder="Short identifier (e.g. fail-1)">
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
        <p class="admin-muted">The governing laws we accept. The AI review uses this list to decide whether a document's chosen law is approved, to pair each law with its court / forum, and to offer Governing Law redline options. The court / forum names the venue that must go with each law, and is required to publish.</p>
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
      // _aiDraftedFields is a transient FE-only marker recording which fields were
      // filled by an applied AI suggestion (drives the "AI-drafted" hint). It is
      // not a valid clause field, so strip it before the payload reaches the gate.
      if (clause && Object.prototype.hasOwnProperty.call(clause, "_aiDraftedFields")) {
        delete clause._aiDraftedFields;
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

  // Policy Version History + Restore is GLOBAL (it applies to the whole playbook,
  // not one clause), so it now lives at the playbook level in #playbookHistory --
  // a sibling of the clause list, outside the per-clause editor. Rendered on every
  // clause re-render so the restore-disabled state stays in step with the draft.
  // Built defensively: a no-op if the host node is absent (older shell / a test
  // mounting only the clause detail).
  function playbookHistoryNode() {
    if (typeof document === "undefined") return null;
    return document.querySelector("#playbookHistory");
  }

  function renderPlaybookLevelHistory() {
    const node = playbookHistoryNode();
    if (!node) return;
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
    node.innerHTML = `
      <section class="admin-special admin-history" data-playbook-history="1">
        <h3>Policy Version History</h3>
        <p class="admin-muted">Every published Playbook stores a restorable snapshot. Restore loads a version into the draft and is disabled while there are unsaved changes.</p>
        ${rows || '<p class="admin-muted">No published policy versions yet.</p>'}
      </section>
    `;
    node.querySelectorAll("[data-restore-playbook-version]").forEach((button) => {
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
    // controls onto the EXISTING loaded option objects, never replacing them
    // outright. The backend authors per-option fields the FE has no control for --
    // forum_jurisdiction (the law<->court pairing the AI forum check + generation
    // read), aliases, entity_prefixes (the governing-law checker's recognition
    // terms). Replacing the array would strip those before the POST, and the
    // backend carry-over could not recover what never arrived.
    //
    // Resolve priors ID-FIRST, with position only as the rename fallback:
    //   * id-match (primary): optionIdForLaw() is order-independent, so it pairs
    //     each law with its OWN forum/aliases correctly under reorder, mid-list
    //     insert, and mid-list delete (where surviving laws shift slot).
    //   * position-match (fallback): only used when the id no longer matches --
    //     the pure-RENAME case, where the slugified label changes (e.g.
    //     "Ontario, Canada" -> "Ontario") but the law keeps its slot. Both lists
    //     are in approved_laws order, so position recovers the rename.
    // Position-FIRST would cross-wire a neighbour's forum on insert/delete (a
    // wrong court before the POST), so it must NOT be the primary key.
    const existingOptions = Array.isArray(rules.approved_options) ? rules.approved_options : [];
    const existingById = {};
    const priorIdAtIndex = [];
    existingOptions.forEach((option, position) => {
      if (!option || typeof option !== "object") return;
      const id = String(option.id || optionIdForLaw(option.value || option.label || "")).trim();
      priorIdAtIndex[position] = id;
      if (id && !(id in existingById)) existingById[id] = option;
    });
    const forumByOptionId = (clause._forumByOptionId && typeof clause._forumByOptionId === "object")
      ? clause._forumByOptionId
      : {};
    // Two-pass resolution mirroring the backend: id-match is primary (safe under
    // reorder/insert/delete); the same-slot position prior is the rename fallback,
    // taken ONLY when that prior id is not still owned by a surviving law -- so a
    // rename+reorder never grafts a still-present law's option onto the renamed one.
    const claimedPriorIds = new Set(
      approved.map((law) => optionIdForLaw(law)).filter((id) => id in existingById),
    );
    rules.approved_options = approved.map((law, index) => {
      const id = optionIdForLaw(law);
      const byIndex = existingOptions[index];
      let prior = existingById[id];
      if (!prior && !claimedPriorIds.has(priorIdAtIndex[index])) {
        prior = (byIndex && typeof byIndex === "object") ? byIndex : undefined;
      }
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

  return {
    loadPlaybook,
    renderClauseDetail,
    renderPlaybookList,
    // Exposed for unit tests: the pure governing-law option normalizer. It mutates
    // only the passed clause object (no DOM), so it is safe to call directly.
    syncGoverningLawRules,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { createPlaybookController };
}
