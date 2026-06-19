// "Generator" tab outbound-draft intake controller.
//
// The NDA Generator is its own top-nav tab (data-view="generator"). The user
// fills the intake form to start a new outbound NDA. The priority is the entity
// picker's "optionality" — picking one of our signing entities pre-fills its
// legal name, address and governing law as a single coupled bundle (so the UK
// entity can never end up on Delaware law), while the governing law stays
// independently overridable (an escape hatch) and the two-address entity lets
// the user choose which address signs.
//
// All pure logic lives in static/js/modules/draft-intake.mjs and is exercised by
// the frontend tests; this controller only renders DOM and dispatches to those
// helpers. "Generate NDA" POSTs buildDraftPayload's output through the injected
// onGenerate seam (wired in app.js to POST /api/generate-nda and download the
// resulting DOCX / show the saved artifact). When onGenerate is absent the
// controller falls back to the legacy stub copy, and when the endpoint is not
// deployed on the running base it degrades to a "pending" notice rather than a
// hard error — the same graceful fallback the entity picker uses for its feed.
function createDraftIntakeController({
  form,
  entitySelect,
  addressField,
  addressSelect,
  bundleNode,
  counterpartyNameInput,
  counterpartyEmailInput,
  ndaTypeSelect,
  termInput,
  // Term stepper controls (the integer-years +/- widget). termInput is the
  // editable number box; the buttons step it; the hint surfaces the playbook cap.
  termDecrementButton,
  termIncrementButton,
  termHintNode,
  termUnitNode,
  projectPurposeInput,
  governingLawSelect,
  lawStatusNode,
  lawResetButton,
  statusNode,
  clearButton,
  // "Start a new NDA": resets the intake form AND clears the document editor so the
  // user can draft the next NDA from a clean slate (the plain Clear button only
  // resets the form). Optional; absent = the button just isn't wired.
  newNdaButton,
  generateButton,
  sideEntityNode,
  sideLawNode,
  sideTypeNode,
  // Optional live-preview surface. When present, the controller renders a
  // readable NDA draft into it on every field change so the user watches the
  // agreement build out. Display-only; "Generate NDA" stays authoritative.
  previewNode,
  // Counterparty detail fields — fill the FIRST-PARTY [SLOT]s in the live preview.
  counterpartyIncorporationInput,
  counterpartyAddressInput,
  businessDescriptionInput,
  // Staged post-generation actions: enabled only after a successful generate.
  downloadButton,
  sendButton,
  onDownloadGenerated,
  onSendGenerated,
  onEditGenerated,
  // Optional seam: notified whenever the staged post-generation actions change —
  // the saved-artifact handle after a successful generate, or null on
  // clear/reset. app.js uses it to reveal/hide the "Send for Signature" CTA in
  // step with whether a sendable generated matter exists. Additive; absent = no-op.
  onStagedActionsChanged,
  // Optional seam: a stubbed generation handler. When the Generic NDA template
  // ships, app.js can pass a real one; until then the default reports pending.
  onGenerate,
  // Optional seam: fire a transient SUCCESS toast on a finished generation. When
  // present, a successful generate routes its confirmation to this toast and the
  // green inline #draftIntakeStatus success text is suppressed; the inline status
  // is kept only for in-progress ("Generating…") and ERROR messages. Absent = the
  // success confirmation falls back to the inline status (graceful degradation).
  notifyGenerated,
  // Optional registry override. When absent the controller first tries the live
  // GET /api/signing-entities feed and falls back to the embedded mirror.
  registryEntities,
  // The signing-entities endpoint. Overridable for tests; defaults to the live
  // route entity-model ships.
  signingEntitiesUrl = "/api/signing-entities",
}) {
  // The pure helper surface, bound to the registry. createDraftIntake comes from
  // the bridged module (window.createDraftIntake); resolved lazily inside the
  // controller so the slightly-later module availability is never read at the
  // classic-script load time this controller is constructed at.
  let intakeApi = null;
  let intake = null;
  let busy = false;
  let initialized = false;
  let registryLoaded = false;

  // The playbook's term cap. Generation clamps a longer requested term down to
  // this (nda_generation._resolve_term_years), so the preview must clamp too or
  // it would show a term the generated NDA won't honour. Defaults to the playbook
  // value (5) and is refreshed from the live /api/signing-entities feed when it
  // carries `playbook_meta.max_term_years` (a backend teammate adds it in
  // parallel). Mirrors DEFAULT_MAX_TERM_YEARS in modules/draft-intake.mjs.
  const DEFAULT_MAX_TERM_YEARS = 5;
  let maxTermYears = DEFAULT_MAX_TERM_YEARS;

  // The term the stepper starts at (and resets to). 2 years matches the form's
  // previous "e.g. 2 years" default; always within [1, maxTermYears].
  const DEFAULT_TERM_YEARS = 2;

  function api() {
    if (!intakeApi) {
      rebindRegistry(registryEntities);
    }
    return intakeApi;
  }

  // (Re)binds the helper surface to a set of entities (and optional playbook law
  // options) and resets the working intake. Used for the initial bind, an explicit
  // registryEntities override, and after the live feed loads.
  function rebindRegistry(entities, lawOptions) {
    const config = {};
    if (entities) config.entities = entities;
    if (Array.isArray(lawOptions) && lawOptions.length) config.lawOptions = lawOptions;
    intakeApi = window.createDraftIntake(config);
    intake = intakeApi.createInitialIntake();
  }

  // Loads the live signing-entity bundles once. The embedded mirror is the
  // fallback, so a 404 (endpoint not deployed yet) or a network error leaves the
  // picker fully functional on the embedded copy rather than breaking the form.
  async function loadRegistry() {
    if (registryLoaded || registryEntities) {
      registryLoaded = true;
      return;
    }
    try {
      const response = await fetch(signingEntitiesUrl, { headers: { Accept: "application/json" } });
      if (response.ok) {
        const payload = await response.json();
        const entities =
          Array.isArray(payload?.entities) && payload.entities.length ? payload.entities : null;
        // Source the governing-law dropdown choices from the playbook (the feed's
        // governing_law_options) so the override list is playbook-driven rather
        // than derived from the embedded entity mirror.
        const lawOptions = Array.isArray(payload?.governing_law_options)
          ? payload.governing_law_options
          : null;
        if (entities || (Array.isArray(lawOptions) && lawOptions.length)) {
          rebindRegistry(entities, lawOptions);
        }
        // Source the playbook term cap from the feed when present so the preview's
        // clamp tracks the live playbook rather than the hardcoded fallback.
        const liveMax = Number(payload?.playbook_meta?.max_term_years);
        if (Number.isFinite(liveMax) && liveMax >= 1) {
          maxTermYears = Math.floor(liveMax);
        }
      }
    } catch (error) {
      // Stay on the embedded mirror; the picker remains usable offline.
    } finally {
      registryLoaded = true;
    }
  }

  clearButton?.addEventListener("click", () => resetForm());
  // "Start a new NDA" wipes the form AND the document editor for the next one.
  newNdaButton?.addEventListener("click", () => startNewNda());

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await generate();
  });

  entitySelect?.addEventListener("change", () => {
    intake = api().applyEntitySelection(intake, entitySelect.value);
    renderEntityBundle();
    renderGoverningLaw();
    setStatus("");
    updateGenerateState();
    flashField("signing_entity");
    flashField("governing_law");
  });

  addressSelect?.addEventListener("change", () => {
    intake = api().selectAddress(intake, addressSelect.value);
    renderEntityBundle();
    flashField("signing_entity");
  });

  governingLawSelect?.addEventListener("change", () => {
    api();
    const entity = intakeApi.selectedEntity(intake);
    const entityLawId = entity ? intakeApi.lawOptionId(entity.governing_law) : null;
    if (entity && governingLawSelect.value === entityLawId) {
      // Choosing the entity's own law re-couples rather than counting as an
      // override, so re-picking the entity later won't be treated as drift.
      intake = intakeApi.clearGoverningLawOverride(intake);
    } else {
      intake = intakeApi.setGoverningLawOverride(intake, governingLawSelect.value);
    }
    renderLawStatus();
    renderSidePanel();
    flashField("governing_law");
  });

  lawResetButton?.addEventListener("click", () => {
    intake = api().clearGoverningLawOverride(intake);
    renderGoverningLaw();
  });

  ndaTypeSelect?.addEventListener("change", () => {
    intake = { ...intake, ndaType: ndaTypeSelect.value };
    renderSidePanel();
  });

  counterpartyNameInput?.addEventListener("input", () => {
    intake = { ...intake, counterpartyName: counterpartyNameInput.value };
    setStatus("");
    updateGenerateState();
    renderSidePanel();
    flashField("counterparty_name");
  });

  counterpartyEmailInput?.addEventListener("input", () => {
    intake = { ...intake, counterpartyEmail: counterpartyEmailInput.value };
    setStatus("");
    updateGenerateState();
    updateSendState();
  });

  // ── Term stepper ──────────────────────────────────────────────────────────
  // The Term field is an integer-years +/- stepper. The minimum is 1 year and the
  // MAXIMUM is the playbook's term cap (maxTermYears, sourced live from the
  // /api/signing-entities feed's playbook_meta.max_term_years; falls back to the
  // hardcoded DEFAULT_MAX_TERM_YEARS only when the feed is unavailable). The value
  // is always clamped to [1, maxTermYears] — the same window generation enforces
  // (nda_generation._resolve_term_years) — so the form can never request a term the
  // generated NDA won't honour. intake.term carries "N years" text, which the
  // backend term_years() parser reads as its leading integer.
  const TERM_MIN_YEARS = 1;

  // The current integer term, derived from intake.term (mirrors the backend's
  // leading-token parse). Falls back to the default when blank/unparseable.
  function currentTermYears() {
    const token = String(intake.term || "").trim().split(/\s+/)[0];
    const parsed = Number.parseInt(token, 10);
    return Number.isFinite(parsed) ? parsed : DEFAULT_TERM_YEARS;
  }

  // Clamp to [1, playbook max].
  function clampTermYears(value) {
    const n = Number.isFinite(value) ? Math.floor(value) : DEFAULT_TERM_YEARS;
    return Math.min(Math.max(n, TERM_MIN_YEARS), maxTermYears);
  }

  // Commit an integer term into the intake (as "N years" text), reflect it in the
  // input, update the +/- disabled state + cap hint, and re-render the dependents.
  function setTermYears(value, { flash = false } = {}) {
    const years = clampTermYears(value);
    const unit = years === 1 ? "year" : "years";
    intake = { ...intake, term: `${years} ${unit}` };
    if (termInput && Number(termInput.value) !== years) termInput.value = String(years);
    if (termUnitNode) termUnitNode.textContent = unit;
    renderTermControls();
    renderSidePanel();
    if (flash) flashField("term");
  }

  // Disable +/- at the bounds and surface the playbook cap once the live value is
  // known (so the user understands why the plus button stops at the max).
  function renderTermControls() {
    const years = currentTermYears();
    if (termInput) {
      termInput.min = String(TERM_MIN_YEARS);
      termInput.max = String(maxTermYears);
    }
    if (termDecrementButton) termDecrementButton.disabled = years <= TERM_MIN_YEARS;
    if (termIncrementButton) termIncrementButton.disabled = years >= maxTermYears;
    if (termHintNode) {
      const unit = maxTermYears === 1 ? "year" : "years";
      termHintNode.textContent = `Playbook caps the term at ${maxTermYears} ${unit}.`;
    }
  }

  termDecrementButton?.addEventListener("click", () => {
    setTermYears(currentTermYears() - 1, { flash: true });
  });

  termIncrementButton?.addEventListener("click", () => {
    setTermYears(currentTermYears() + 1, { flash: true });
  });

  // Typing directly into the box: re-clamp on every input so an out-of-range value
  // (e.g. 99) is corrected to the cap immediately and the +/- state stays honest.
  termInput?.addEventListener("input", () => {
    const parsed = Number.parseInt(termInput.value, 10);
    setTermYears(Number.isFinite(parsed) ? parsed : DEFAULT_TERM_YEARS);
  });

  projectPurposeInput?.addEventListener("input", () => {
    intake = { ...intake, projectPurpose: projectPurposeInput.value };
    renderSidePanel();
    flashField("project_purpose");
  });

  counterpartyIncorporationInput?.addEventListener("input", () => {
    intake = { ...intake, counterpartyIncorporation: counterpartyIncorporationInput.value };
    renderSidePanel();
    flashField("counterparty_incorporation");
  });

  counterpartyAddressInput?.addEventListener("input", () => {
    intake = { ...intake, counterpartyAddress: counterpartyAddressInput.value };
    renderSidePanel();
    flashField("counterparty_address");
  });

  businessDescriptionInput?.addEventListener("input", () => {
    intake = { ...intake, businessDescription: businessDescriptionInput.value };
    renderSidePanel();
    flashField("business_description");
  });

  // The last successful generation, used by the Download/Send actions.
  let lastGenerated = null;

  // Download + Send are always available (not staged). Download needs the actual
  // document, so it generates first (if needed) then downloads.
  downloadButton?.addEventListener("click", async () => {
    if (!lastGenerated) await generate();
    if (lastGenerated && typeof onDownloadGenerated === "function") {
      onDownloadGenerated(lastGenerated, { sourceButton: downloadButton });
    }
  });

  // Send ALWAYS opens the email popup immediately — never blocked on generation —
  // with the recipient prefilled from the counterparty email. If no NDA exists
  // yet, it generates in the background and attaches the document to the open
  // modal when ready (the modal's own "Send document" stays disabled until a
  // document is attached, so nothing can be sent empty).
  sendButton?.addEventListener("click", async () => {
    if (typeof onSendGenerated !== "function") return;
    // Already generated -> open the popup and attach the NDA straight away.
    if (lastGenerated) {
      onSendGenerated(lastGenerated);
      return;
    }
    // Not generated yet -> validate first so we don't open an empty popup, then
    // open it (recipient prefilled + "attaching…"), generate, and attach.
    const result = api().validateDraftIntake(intake);
    if (!result.ok) {
      setStatus(result.error, "error");
      return;
    }
    onSendGenerated(currentSendContext(), { pending: true });
    await generate();
    if (lastGenerated) onSendGenerated(lastGenerated);
  });

  // The handle Send acts on: the last generation if present, otherwise a draft
  // context carrying just the recipient + subject so the popup can open + prefill
  // immediately, before any document exists.
  function currentSendContext() {
    if (lastGenerated) return lastGenerated;
    const name = intake && intake.counterpartyName ? intake.counterpartyName.trim() : "";
    return {
      counterpartyEmail: (intake && intake.counterpartyEmail) || "",
      subject: name ? `NDA — ${name}` : "NDA",
    };
  }

  // Records the last successful generation so Download/Send act on it, and
  // notifies the optional staged-actions seam (app.js reveals/hides the
  // "Send for Signature" CTA from this) with the saved-artifact handle or null.
  function setStagedActions(generated) {
    lastGenerated = generated || null;
    if (typeof onStagedActionsChanged === "function") onStagedActionsChanged(lastGenerated);
  }

  // One-time population of the static option lists (entities, NDA types, laws).
  // Loads the live registry first so the option lists reflect the deployed
  // bundles; the embedded mirror backs it up if the feed is unavailable.
  async function ensureInitialized() {
    if (initialized) return;
    await loadRegistry();
    api();
    populateEntityOptions();
    populateNdaTypeOptions();
    populateGoverningLawOptions();
    // loadRegistry() may have refreshed maxTermYears from the live playbook feed;
    // seed the stepper to the default term, re-clamped against the live cap.
    setTermYears(currentTermYears());
    initialized = true;
  }

  function populateEntityOptions() {
    if (!entitySelect) return;
    const placeholder = entitySelect.querySelector("option[value='']");
    entitySelect.innerHTML = "";
    if (placeholder) entitySelect.appendChild(placeholder);
    for (const entity of intakeApi.entities) {
      const option = document.createElement("option");
      option.value = entity.id;
      option.textContent = intakeApi.entityLabel(entity);
      entitySelect.appendChild(option);
    }
  }

  function populateNdaTypeOptions() {
    if (!ndaTypeSelect) return;
    ndaTypeSelect.innerHTML = "";
    for (const type of intakeApi.ndaTypes) {
      const option = document.createElement("option");
      option.value = type.id;
      option.textContent = type.label;
      ndaTypeSelect.appendChild(option);
    }
    ndaTypeSelect.value = intake.ndaType;
  }

  function populateGoverningLawOptions() {
    if (!governingLawSelect) return;
    governingLawSelect.innerHTML = "";
    for (const law of intakeApi.governingLawOptions()) {
      const option = document.createElement("option");
      option.value = law.id;
      option.textContent = law.label;
      governingLawSelect.appendChild(option);
    }
  }

  function renderEntityBundle() {
    const entity = intakeApi.selectedEntity(intake);
    renderAddressField(entity);
    if (bundleNode) {
      const hasEntity = Boolean(entity);
      bundleNode.classList.toggle("empty", !hasEntity);
      if (!hasEntity) {
        bundleNode.textContent = "Pick an entity to pre-fill its legal name, address and governing law.";
      } else {
        const address = intakeApi.selectedAddress(intake);
        const law = intakeApi.effectiveGoverningLaw(intake);
        bundleNode.innerHTML = `
          <dl class="draft-bundle-grid">
            <div><dt>Legal name</dt><dd>${escapeHtml(entity.legal_name)}</dd></div>
            <div><dt>${escapeHtml(address?.label || "Address")}</dt><dd>${escapeHtml(intakeApi.formatAddressLines(address))}</dd></div>
            <div><dt>Governing law</dt><dd>${escapeHtml(law?.label || "—")}</dd></div>
          </dl>
        `;
      }
    }
    renderSidePanel();
  }

  // The address picker is only meaningful for the multi-address entity; for a
  // single-address entity it is hidden (there is nothing to choose).
  function renderAddressField(entity) {
    if (!addressField || !addressSelect) return;
    const multi = Boolean(entity) && intakeApi.hasMultipleAddresses(entity);
    addressField.hidden = !multi;
    if (!multi) {
      addressSelect.innerHTML = "";
      return;
    }
    addressSelect.innerHTML = "";
    for (const address of entity.addresses) {
      const option = document.createElement("option");
      option.value = address.id;
      option.textContent = `${address.label} — ${intakeApi.formatAddressLines(address)}`;
      addressSelect.appendChild(option);
    }
    addressSelect.value = intake.addressId || (intakeApi.defaultAddressFor(entity)?.id ?? "");
  }

  function renderGoverningLaw() {
    if (governingLawSelect) {
      const law = intakeApi.effectiveGoverningLaw(intake);
      if (law) governingLawSelect.value = law.id;
    }
    renderLawStatus();
    renderSidePanel();
  }

  function renderLawStatus() {
    const entity = intakeApi.selectedEntity(intake);
    if (lawResetButton) lawResetButton.hidden = !intake.governingLawOverridden;
    if (!lawStatusNode) return;
    if (!entity) {
      lawStatusNode.textContent = "Defaults to the entity's law once an entity is picked.";
      return;
    }
    if (intake.governingLawOverridden) {
      lawStatusNode.textContent = `Overridden — independent of ${intakeApi.entityLabel(entity)}.`;
    } else {
      lawStatusNode.textContent = `Coupled to ${intakeApi.entityLabel(entity)}.`;
    }
  }

  // Briefly pulses the preview span(s) for `fieldKey` so the user sees the exact
  // text that their control just changed. Works without new CSS rules: sets an
  // inline background/box-shadow on matching [data-generator-field] elements,
  // then clears it after ~900 ms. If previewNode is absent the call is a no-op.
  function flashField(fieldKey) {
    if (!previewNode || !fieldKey) return;
    const spans = previewNode.querySelectorAll(`[data-generator-field="${fieldKey}"]`);
    spans.forEach((span) => {
      // Brighter violet burst on top of the existing fill colour; outline makes it
      // visible on blank placeholders too (which use amber bg, not violet).
      span.style.outline = "2px solid #8b5cf6";
      span.style.outlineOffset = "2px";
      span.style.background = "#ddd6fe"; // one step deeper than --violet-bg
      span.style.transition = "background 900ms ease, outline-color 900ms ease";
    });
    setTimeout(() => {
      spans.forEach((span) => {
        span.style.outline = "";
        span.style.outlineOffset = "";
        span.style.background = "";
        span.style.transition = "";
      });
    }, 900);
  }

  function renderSidePanel() {
    const entity = intakeApi.selectedEntity(intake);
    const law = intakeApi.effectiveGoverningLaw(intake);
    const type = intakeApi.ndaTypes.find((item) => item.id === intake.ndaType);
    if (sideEntityNode) sideEntityNode.textContent = entity ? intakeApi.entityLabel(entity) : "—";
    if (sideLawNode) sideLawNode.textContent = law ? law.label : "—";
    if (sideTypeNode) sideTypeNode.textContent = type ? type.label : "—";
    renderLivePreview();
  }

  // Assembles a readable NDA draft from the current intake and writes it into the
  // preview pane. Re-rendered on every change so the document visibly fills in as
  // the user picks an entity, names the counterparty, sets the term, etc. This is
  // an illustrative client-side preview; "Generate NDA" produces the authoritative
  // server-rendered .docx with the full playbook clause wording.
  function renderLivePreview() {
    if (!previewNode) return;
    const entity = intakeApi.selectedEntity(intake);
    const address = intakeApi.selectedAddress(intake);
    const law = intakeApi.effectiveGoverningLaw(intake);

    // Inserted/filled values — the signing-entity side (legal name, incorporation,
    // address, coupled governing law, signatory) AND the counterparty name /
    // incorporation / registered office, the recital business description, the
    // project/purpose and the term — all get an `nda-fill-entity` marker so they
    // read as highlighted in the viewer AND survive into the always-visible editor
    // (generator-editor.js carries only `.nda-fill-entity` through its run model;
    // a plain `.nda-fill` highlight is flattened away on showDraft). A visual aid
    // only: the marker is carried through the editor's run model and dropped on
    // export, so it never reaches the generated/sent document.
    // fieldKey links the span to its controlling form field for flashField().
    const entityField = (value, placeholder, fieldKey) => {
      const attr = fieldKey ? ` data-generator-field="${escapeHtml(fieldKey)}"` : "";
      return value && String(value).trim()
        ? `<span class="nda-fill nda-fill-entity"${attr}>${escapeHtml(String(value).trim())}</span>`
        : `<span class="nda-blank"${attr}>[${escapeHtml(placeholder)}]</span>`;
    };

    const asporaName = entity ? entity.legal_name : null;
    const asporaShort = entity ? entity.short_name || "Aspora" : "Aspora";
    const asporaIncorp = entity ? entity.incorporation_jurisdiction : null;
    const asporaAddr = entity ? intakeApi.formatAddressLines(address) : null;
    const sig = (entity && entity.signatory) || {};
    const governingLaw = law ? law.label : null;
    // The generated NDA's governing-law clause names BOTH the governing law AND the
    // forum/court: "...the laws of [LAW], and [FORUM] shall have exclusive
    // jurisdiction ..." (nda_generation._fill_variable_slots). The forum is sourced
    // from the SAME registry the backend uses (the entity's `jurisdiction` field via
    // intakeApi.effectiveForum) — never a duplicated FE list — and tracks the
    // OVERRIDDEN law on an override, so the preview is what-you-see = what-you-get.
    const forum = intakeApi.effectiveForum(intake);

    // Term clamp preview. Generation parses the typed term to a year count exactly
    // like nda_generation_workflow.term_years (first whitespace token -> int, else
    // a default) and then clamps to [1, max_term_years] (_resolve_term_years). The
    // raw preview echoed intake.term verbatim, so "10 years" previewed as 10 but
    // generates as the cap. We mirror that parse+clamp here and, when the typed
    // term was actually reduced, show an inline note so the cap is visible.
    const rawTerm = String(intake.term || "").trim();
    const parseTermYears = (value) => {
      // Mirror nda_generation_workflow.term_years: first whitespace-delimited
      // token, parsed as an integer; non-numeric/blank falls back to null here so
      // the preview leaves the user's wording untouched (we only clamp a term we
      // can actually read as a number, e.g. "10" or "10 years").
      const token = value.split(/\s+/)[0];
      if (!/^\d+$/.test(token)) return null;
      const parsed = Number.parseInt(token, 10);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const typedYears = parseTermYears(rawTerm);
    let termDisplay = rawTerm; // verbatim when not a plain number of years
    let termClampNote = "";
    if (typedYears !== null) {
      const clamped = Math.min(Math.max(typedYears, 1), maxTermYears);
      const unit = clamped === 1 ? "year" : "years";
      termDisplay = `${clamped} ${unit}`;
      if (clamped !== typedYears) {
        termClampNote = ` <span class="nda-doc-note" style="color:var(--ink-muted);font-size:0.85em;font-style:italic;">(capped to ${maxTermYears} ${
          maxTermYears === 1 ? "year" : "years"
        } per playbook)</span>`;
      }
    }

    const ordinal = (n) => {
      const s = ["th", "st", "nd", "rd"];
      const v = n % 100;
      return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`;
    };
    const now = new Date();
    const dateStr = `${ordinal(now.getDate())} day of ${now.toLocaleString("en-GB", {
      month: "long",
    })}, ${now.getFullYear()}`;

    // Signature-block fields go through entityField() too so they read like the
    // rest of the document: a filled value gets the violet `.nda-fill` highlight,
    // an empty one the amber `.nda-blank` placeholder — consistent everywhere.
    // The counterparty name reuses its form field key (so it pulses with the same
    // control as the recital); the signatory name/title have no dedicated control,
    // so they pass a null fieldKey and carry no data-generator-field.
    const counterpartyLabel = entityField(intake.counterpartyName, "Company name", "counterparty_name");
    const sigName = entityField(sig.name, "Authorised Signatory", null);
    const sigTitle = entityField(sig.title, "Title", null);

    previewNode.innerHTML = `
      <article class="nda-doc">
        <p class="nda-doc-kicker">Draft &middot; ${escapeHtml(asporaShort)} paper &middot; Generic NDA</p>
        <h3 class="nda-doc-title">NON-DISCLOSURE AGREEMENT</h3>

        <p>This Non-Disclosure Agreement (&ldquo;<b>Agreement</b>&rdquo;) is made on this ${escapeHtml(dateStr)} by and between:</p>

        <p>${entityField(intake.counterpartyName, "Company name", "counterparty_name")}, a company incorporated under the laws of ${entityField(intake.counterpartyIncorporation, "jurisdiction of incorporation", "counterparty_incorporation")}, having its registered office at ${entityField(intake.counterpartyAddress, "registered office address", "counterparty_address")} (hereinafter the &ldquo;<b>Company</b>&rdquo;, which includes its successors and permitted assigns) of the FIRST PARTY.</p>

        <p class="nda-doc-and">AND</p>

        <p>${entityField(asporaName, "Aspora signing entity", "signing_entity")}, a company incorporated under the laws of ${entityField(asporaIncorp, "jurisdiction of incorporation", "signing_entity")}, having its registered office at ${entityField(asporaAddr, "registered office address", "signing_entity")} (hereinafter &ldquo;<b>Aspora</b>&rdquo;, which includes its successors and permitted assigns) of the SECOND PARTY.</p>

        <p>The Company and Aspora are collectively the &ldquo;<b>Parties</b>&rdquo; and individually a &ldquo;<b>Party</b>&rdquo;. The Party disclosing Information is the &ldquo;<b>Disclosing Party</b>&rdquo; and the Party receiving it is the &ldquo;<b>Receiving Party</b>&rdquo;.</p>

        <p class="nda-doc-recital-h">WHEREAS:</p>
        <p class="nda-doc-recital">(A)&nbsp;&nbsp;The Company is involved in the business of ${entityField(intake.businessDescription, "business description", "business_description")}.</p>
        <p class="nda-doc-recital">(B)&nbsp;&nbsp;The Parties intend to enter discussions regarding ${entityField(intake.projectPurpose, "certain commercial propositions", "project_purpose")} (the &ldquo;<b>Purpose</b>&rdquo;); and</p>
        <p class="nda-doc-recital">(C)&nbsp;&nbsp;To proceed with the Purpose, the Disclosing Party has agreed to exchange certain Confidential Information on a strictly confidential basis on the terms of this Agreement.</p>

        <p>IN CONSIDERATION of the Purpose and for other good and valuable consideration (the receipt and sufficiency of which is acknowledged), each Party agrees as follows:</p>

        <ol class="nda-clauses">
          <li><b>No obligation.</b> The Disclosing Party is under no obligation to disclose any additional documents, papers or Confidential Information, save and except what it in its discretion deems necessary for the Purpose.</li>

          <li><b>Confidential Information.</b> &ldquo;Confidential Information&rdquo; means any and all information and/or data obtained &mdash; whether in writing, pictorially, in machine-readable form, orally or by observation during visits &mdash; in connection with the Purpose or otherwise, including but not limited to financial information, business reports, account books, profit and loss statements, digital or other content, know-how, processes, trade secrets, schematics, technology, technical and research information, procedures, algorithms, data, designs, business and operational information, planning, marketing interests, merchandising, packaging, advertising, customer, employee and supplier information, sales statistics, pricing, market intelligence, strategies, and the existence of this Agreement, whether or not designated as confidential.</li>

          <li><b>Exceptions to Confidential Information.</b> Confidential Information does not include information that:
            <ol class="nda-subclauses">
              <li>is or becomes publicly available without breach of this Agreement;</li>
              <li>becomes lawfully available to either Party from a third party free from any confidentiality restriction; or</li>
              <li>was previously in the Receiving Party&rsquo;s possession and was not acquired, directly or indirectly, from the Disclosing Party, as evidenced by written records.</li>
            </ol>
          </li>

          <li><b>Use and non-disclosure.</b> The Receiving Party agrees that the Confidential Information will be:
            <ol class="nda-subclauses">
              <li>used solely for the Purpose and not in any way, directly or indirectly, detrimental to the Disclosing Party or to procure a commercial advantage over it;</li>
              <li>treated with at least the same degree of care as the Receiving Party&rsquo;s own Confidential Information, without modifying or erasing any logos or trademarks; and</li>
              <li>kept strictly confidential and not disclosed to any person without the Disclosing Party&rsquo;s prior written consent.</li>
            </ol>
          </li>

          <li><b>Permitted disclosures.</b> The Receiving Party may disclose Confidential Information to its directors, officers, consultants, advisers, employees and staff (&ldquo;Representatives&rdquo;) who need to know it for the Purpose, having informed them of its confidential nature and bound them to equivalent obligations; the Receiving Party is responsible for any breach by its Representatives. Disclosure may also be made where legally compelled, on prompt notice to the Disclosing Party and, where possible, the opportunity to contest, limited to the extent required.</li>

          <li><b>Copies.</b> The Receiving Party will not copy or reproduce (including storing in any computer or electronic system) any Confidential Information except for the Purpose without prior written consent; all copies are returned or destroyed on expiry or termination.</li>

          <li><b>Intellectual property rights.</b> The Receiving Party acquires no intellectual property rights under this Agreement or any disclosure hereunder, except the limited right to use the Confidential Information in accordance with the Purpose.</li>

          <li><b>Remedies for breach.</b> The Receiving Party acknowledges that damages are not a sufficient remedy and that the Disclosing Party is entitled to specific performance or injunctive relief for any breach or threatened breach, in addition to any other remedy available at law or in equity.</li>

          <li><b>Confirmations.</b> The Disclosing Party confirms that, by disclosing the Confidential Information, it has not breached any confidentiality obligation owed to any other party.</li>

          <li><b>No warranties.</b> Save as expressly provided, no warranties of any kind are given with respect to the Confidential Information; in no event is either Party liable for loss of profits or business, or for any direct, indirect, special or consequential damages arising out of the Confidential Information or its use.</li>

          <li><b>Return of Confidential Information.</b> On expiry or on the Disclosing Party&rsquo;s request, the Receiving Party will deliver and return all copies of Confidential Information in its possession or control, or with written consent erase and/or destroy it and certify the destruction in writing. The confidentiality requirements survive the return or destruction of the Confidential Information.</li>

          <li><b>Entire agreement; waiver and modification.</b> This Agreement supersedes all prior discussions and writings and is the entire agreement on its subject matter. No waiver or modification binds either Party unless made in writing and signed by a duly authorised representative of each Party; no failure or delay in exercising any right operates as a waiver.</li>

          <li><b>Governing law and jurisdiction.</b> This Agreement is governed by and construed in accordance with the laws of ${entityField(governingLaw, "governing law", "governing_law")}, and ${entityField(forum, "the competent courts", "governing_law")} shall have exclusive jurisdiction over any dispute arising out of or in connection with this Agreement.</li>

          <li><b>Severability.</b> If any provision is held unenforceable by a court or tribunal of competent jurisdiction, the remaining provisions remain in full force and effect.</li>

          <li><b>Term.</b> This Agreement is effective on the date of signing and remains in force until the earlier of (i) completion of the Purpose, or (ii) ${entityField(termDisplay, "two (2) years", "term")}${termClampNote} from the date of this Agreement.</li>
        </ol>

        <p class="nda-doc-witness">IN WITNESS WHEREOF the Parties, through their Authorised Signatories, have set and subscribed their respective hands and seals the day and year first written above.</p>

        <div class="nda-doc-signoff">
          <div>
            <span class="nda-doc-sig-label">For the Company</span>
            <span class="nda-doc-sig-party">${counterpartyLabel}</span>
            <span class="nda-doc-sig-line"></span>
            <span class="nda-doc-sig-meta">Name &middot; Title &middot; Date</span>
          </div>
          <div>
            <span class="nda-doc-sig-label">For Aspora</span>
            <span class="nda-doc-sig-party">${entityField(asporaName, "Aspora signing entity", "signing_entity")}</span>
            <span class="nda-doc-sig-line"></span>
            <span class="nda-doc-sig-meta">${sigName} &middot; ${sigTitle} &middot; Date</span>
          </div>
        </div>

        <p class="nda-doc-foot">Live preview of the Generic NDA &middot; final wording, dates and signatories are set when you generate.</p>
      </article>
    `;
    // Mirror the draft into the always-visible editor. The editor module guards the
    // "user has edited the draft" and "a real NDA has been generated" cases itself.
    if (window.generatorEditor && typeof window.generatorEditor.showDraft === "function") {
      window.generatorEditor.showDraft(previewNode);
    }
  }

  function updateGenerateState() {
    if (!generateButton) return;
    const result = intakeApi.validateDraftIntake(intake);
    generateButton.disabled = busy || !result.ok;
  }

  // Send EMAILS the NDA to the counterparty, so it needs a counterparty email —
  // the Send modal's own submit stays disabled without a valid recipient. The
  // counterparty email field is otherwise OPTIONAL (Generate / Download / Send for
  // Signature don't need it), so only Send is gated here. We disable the Send
  // button and explain why in its title when no valid email is present, so the
  // "(required to send)" label and the button agree on the real gate.
  function hasSendableEmail() {
    const email = String(intake.counterpartyEmail || "").trim();
    return email !== "" && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
  }

  function updateSendState() {
    if (!sendButton) return;
    const ready = hasSendableEmail();
    sendButton.disabled = !ready;
    sendButton.title = ready ? "Email the NDA" : "Add a counterparty email to send";
  }

  async function generate() {
    if (busy) return;
    const result = api().validateDraftIntake(intake);
    if (!result.ok) {
      setStatus(result.error, "error");
      return;
    }
    busy = true;
    if (clearButton) clearButton.disabled = true;
    updateGenerateState();
    setStatus("Generating NDA…");

    const payload = intakeApi.buildDraftPayload(intake);
    try {
      if (typeof onGenerate === "function") {
        // onGenerate performs the POST + download/save side effects. It may
        // return a {message, toast, tone} to render (e.g. a "pending" notice when
        // the endpoint is not deployed yet); otherwise the default success copy is
        // shown. A thrown error is surfaced in the error tone below.
        const outcome = await onGenerate(payload);
        const tone = outcome?.tone || "success";
        // SUCCESS confirmation moves to a transient green TOAST (notifyGenerated)
        // instead of a persistent green inline status line. We clear the inline
        // status (it last read "Generating…") so no stale text lingers. The inline
        // #draftIntakeStatus is reserved for in-progress + ERROR messages now. If
        // the toast seam is absent (e.g. a test harness), or the outcome carries no
        // toast text (the generation-unavailable pending notice), fall back to the
        // inline status so the message is never silently dropped.
        if (tone === "success" && typeof notifyGenerated === "function" && outcome?.toast) {
          notifyGenerated(outcome.toast);
          setStatus("");
        } else {
          setStatus(outcome?.message || "NDA generated.", tone);
        }
        // A real generation returns the saved-artifact handle; stage Download/Send.
        if (outcome?.generated) setStagedActions(outcome.generated);
        // Load the generated NDA into the always-visible editor automatically (no
        // separate "Edit" step) so its text can be edited immediately in place.
        if (outcome?.generated && typeof onEditGenerated === "function") {
          onEditGenerated(outcome.generated);
        }
      } else {
        // Stub: the Generic NDA template has not arrived. Capture the inputs and
        // tell the user generation is pending rather than pretending to draft.
        setStatus(
          `Captured draft for ${payload.counterparty.name} on ${payload.signing_entity.legal_name} paper. Generation is pending the Generic NDA template.`,
          "success",
        );
      }
    } catch (error) {
      setStatus(error?.message || "Could not generate the NDA.", "error");
    } finally {
      busy = false;
      if (clearButton) clearButton.disabled = false;
      updateGenerateState();
    }
  }

  function resetForm({ status = "" } = {}) {
    api();
    intake = intakeApi.createInitialIntake();
    if (form) form.reset();
    if (entitySelect) entitySelect.value = "";
    if (ndaTypeSelect) ndaTypeSelect.value = intake.ndaType;
    // Re-seed the term stepper to the default (form.reset() restores the input's
    // value but not the intake's term text, which createInitialIntake blanked).
    setTermYears(DEFAULT_TERM_YEARS);
    renderEntityBundle();
    renderGoverningLaw();
    setStagedActions(null);
    setStatus(status, status ? "success" : "");
    updateGenerateState();
    updateSendState();
  }

  // "Start a new NDA": reset the intake form (resetForm) AND clear the always-visible
  // document editor so the just-generated NDA is wiped and the next one starts from a
  // clean, empty slate. The plain Clear button only resets the form; this also empties
  // the editor pane (showing its empty state) so nothing from the previous NDA lingers.
  function startNewNda() {
    resetForm({ status: "Started a new NDA." });
    if (window.generatorEditor && typeof window.generatorEditor.clear === "function") {
      window.generatorEditor.clear();
    }
  }

  // Called when the Generator tab is shown. Loads the live registry on first
  // activation (the empty-state copy shows during the brief async gap, never a
  // blank panel), populates the option lists, and renders the current state. The
  // form persists across tab switches — re-activating does not wipe in-progress
  // input. Idempotent and safe to call on every tab activation.
  async function activate() {
    await ensureInitialized();
    renderEntityBundle();
    renderGoverningLaw();
    updateGenerateState();
    updateSendState();
    renderTermControls();
  }

  function setStatus(message, tone = "") {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.classList.toggle("error", tone === "error");
    statusNode.classList.toggle("success", tone === "success");
  }

  return { activate, resetForm, startNewNda, generate };
}
