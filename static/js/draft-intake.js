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
  projectPurposeInput,
  notesInput,
  governingLawSelect,
  lawStatusNode,
  lawResetButton,
  statusNode,
  clearButton,
  generateButton,
  sideEntityNode,
  sideLawNode,
  sideTypeNode,
  // Optional seam: a stubbed generation handler. When the Generic NDA template
  // ships, app.js can pass a real one; until then the default reports pending.
  onGenerate,
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

  function api() {
    if (!intakeApi) {
      rebindRegistry(registryEntities);
    }
    return intakeApi;
  }

  // (Re)binds the helper surface to a set of entities and resets the working
  // intake. Used for the initial bind, an explicit registryEntities override,
  // and after the live feed loads.
  function rebindRegistry(entities) {
    intakeApi = window.createDraftIntake(entities ? { entities } : {});
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
        if (Array.isArray(payload?.entities) && payload.entities.length) {
          rebindRegistry(payload.entities);
        }
      }
    } catch (error) {
      // Stay on the embedded mirror; the picker remains usable offline.
    } finally {
      registryLoaded = true;
    }
  }

  clearButton?.addEventListener("click", () => resetForm());

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
  });

  addressSelect?.addEventListener("change", () => {
    intake = api().selectAddress(intake, addressSelect.value);
    renderEntityBundle();
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
  });

  counterpartyEmailInput?.addEventListener("input", () => {
    intake = { ...intake, counterpartyEmail: counterpartyEmailInput.value };
    setStatus("");
    updateGenerateState();
  });

  termInput?.addEventListener("input", () => {
    intake = { ...intake, term: termInput.value };
  });

  projectPurposeInput?.addEventListener("input", () => {
    intake = { ...intake, projectPurpose: projectPurposeInput.value };
  });

  notesInput?.addEventListener("input", () => {
    intake = { ...intake, notes: notesInput.value };
  });

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

  function renderSidePanel() {
    const entity = intakeApi.selectedEntity(intake);
    const law = intakeApi.effectiveGoverningLaw(intake);
    const type = intakeApi.ndaTypes.find((item) => item.id === intake.ndaType);
    if (sideEntityNode) sideEntityNode.textContent = entity ? intakeApi.entityLabel(entity) : "—";
    if (sideLawNode) sideLawNode.textContent = law ? law.label : "—";
    if (sideTypeNode) sideTypeNode.textContent = type ? type.label : "—";
  }

  function updateGenerateState() {
    if (!generateButton) return;
    const result = intakeApi.validateDraftIntake(intake);
    generateButton.disabled = busy || !result.ok;
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

    const payload = intakeApi.buildDraftPayload(intake);
    try {
      if (typeof onGenerate === "function") {
        // onGenerate performs the POST + download/save side effects. It may
        // return a {message, tone} to render (e.g. a "pending" notice when the
        // endpoint is not deployed yet); otherwise the default success copy is
        // shown. A thrown error is surfaced in the error tone below.
        const outcome = await onGenerate(payload);
        setStatus(outcome?.message || "NDA generated.", outcome?.tone || "success");
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
    renderEntityBundle();
    renderGoverningLaw();
    setStatus(status, status ? "success" : "");
    updateGenerateState();
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
  }

  function setStatus(message, tone = "") {
    if (!statusNode) return;
    statusNode.textContent = message;
    statusNode.classList.toggle("error", tone === "error");
    statusNode.classList.toggle("success", tone === "success");
  }

  return { activate, resetForm, generate };
}
