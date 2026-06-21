// Entities console: author the signing-entity registry (legal name, addresses,
// governing law joined to the playbook's approved options, court/jurisdiction,
// incorporation, signatory). Mirrors admin-access.js -- a GET probe on load (a
// 403 = "not an admin", rendered as a calm read-only state), an in-memory
// working copy edited card-by-card, and a publish-style Save that POSTs the full
// replacement registry. Governing law is a dropdown JOINED to the playbook's
// approved governing-law options, so an entity can only point at an approved law.
const AdminEntitiesView = (() => {
  function esc(value) {
    if (typeof window !== "undefined" && typeof window.escapeHtml === "function") {
      return window.escapeHtml(value);
    }
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  // A stable, collision-resistant suffix for the per-card default-address radio
  // group name (so cards don't share one radio group). Not security-sensitive.
  let radioGroupSeq = 0;

  function createController({
    panel,
    list,
    message,
    refreshButton,
    addButton,
    saveButton,
    cardTemplate,
    addressTemplate,
  }) {
    // The in-memory working copy. `entities` is an array of plain objects the
    // user edits; `lawOptions` is [{id,label}] sourced from the playbook.
    let lawOptions = [];
    let playbookAvailable = false;
    let dirty = false;
    let loaded = false;

    refreshButton?.addEventListener("click", () => {
      load();
    });
    addButton?.addEventListener("click", () => {
      addEntity();
    });
    saveButton?.addEventListener("click", () => {
      save();
    });

    function setMessage(text, tone) {
      if (!message) return;
      message.textContent = text;
      message.classList.toggle("is-error", tone === "error");
      message.classList.toggle("is-ok", tone === "ok");
    }

    function setDirty(value) {
      dirty = value;
      if (saveButton) saveButton.disabled = !value;
    }

    function setControlsDisabled(disabled) {
      if (addButton) addButton.disabled = disabled;
      if (saveButton) saveButton.disabled = disabled || !dirty;
    }

    async function load() {
      if (!panel) return;
      setMessage("Loading signing entities.");
      try {
        const response = await fetch("/api/admin/signing-entities", {
          headers: { Accept: "application/json" },
        });
        // The registry editor is admin-only. A non-admin still loads the app
        // shell, so a 403 here is expected -- render a calm read-only state.
        if (response.status === 403) {
          renderAdminOnly();
          return;
        }
        const payload = await parseOk(response, "Signing entities could not load");
        applyWorkspace(payload);
      } catch (error) {
        renderError(error.message || "Signing entities could not load");
      }
    }

    async function parseOk(response, fallback) {
      if (typeof window !== "undefined" && window.AuthExpired?.parseOkJson) {
        return window.AuthExpired.parseOkJson(response, fallback);
      }
      if (!response.ok) {
        let detail = fallback;
        try {
          const body = await response.json();
          detail = body?.error || fallback;
        } catch (_) {
          /* keep fallback */
        }
        throw new Error(detail);
      }
      return response.json();
    }

    function renderAdminOnly() {
      loaded = false;
      if (list) list.innerHTML = "";
      if (addButton) addButton.disabled = true;
      if (saveButton) saveButton.disabled = true;
      setMessage("The signing-entity registry is managed by an administrator.");
    }

    function renderError(text) {
      if (addButton) addButton.disabled = true;
      if (saveButton) saveButton.disabled = true;
      setMessage(text, "error");
    }

    function applyWorkspace(payload = {}) {
      loaded = true;
      lawOptions = Array.isArray(payload.governing_law_options)
        ? payload.governing_law_options.filter((o) => o && o.id)
        : [];
      playbookAvailable = Boolean(payload.playbook_available);
      const entities = Array.isArray(payload.entities) ? payload.entities : [];
      renderList(entities);
      if (addButton) addButton.disabled = false;
      setDirty(false);
      if (payload.saved) {
        setMessage("Registry saved.", "ok");
      } else if (!playbookAvailable) {
        setMessage(
          "Playbook unavailable: governing-law options could not be loaded. Saving is still possible but law validation is skipped.",
        );
      } else {
        setMessage(`${entities.length} signing ${entities.length === 1 ? "entity" : "entities"}.`);
      }
    }

    // Rebuild the whole list DOM from the supplied entities. Editing happens in
    // the DOM (inputs are the source of truth); we read them back at save time.
    function renderList(entities) {
      if (!list) return;
      list.innerHTML = "";
      entities.forEach((entity) => {
        list.appendChild(buildCard(entity));
      });
    }

    function buildCard(entity, { isNew = false } = {}) {
      const fragment = cardTemplate.content.cloneNode(true);
      const card = fragment.querySelector("[data-entity-card]");
      const radioGroup = `entity-default-${(radioGroupSeq += 1)}`;

      // The entity id is the persistent key. It stays the under-the-hood identifier
      // (the input is always present so collectEntities reads it back), but it is
      // only SURFACED + editable when ADDING a new entity. For an existing entity it
      // is shown minimally as a small de-emphasised caption, never as an editable row.
      field(card, "id").value = String(entity.id || "");
      field(card, "legal_name").value = String(entity.legal_name || "");
      field(card, "short_name").value = String(entity.short_name || "");
      field(card, "jurisdiction").value = String(entity.jurisdiction || "");
      field(card, "incorporation_jurisdiction").value = String(
        entity.incorporation_jurisdiction || "",
      );
      field(card, "signatory_name").value = String(entity.signatory?.name || "");
      field(card, "signatory_title").value = String(entity.signatory?.title || "");
      field(card, "legal_name-display").textContent =
        String(entity.legal_name || "New entity");

      // Mark whether this card represents an already-persisted entity, so Remove can
      // confirm before deleting one (a new, unsaved card removes silently).
      card.dataset.entityNew = isNew ? "true" : "false";
      applyIdSurface(card, isNew);

      const lawSelect = field(card, "governing_law");
      const currentLaw = String(entity.governing_law?.playbook_option_id || "");
      populateLawSelect(lawSelect, currentLaw);
      updateLawWarning(card, currentLaw);

      const addressList = field(card, "address-list");
      const addresses =
        Array.isArray(entity.addresses) && entity.addresses.length
          ? entity.addresses
          : [{ id: "registered", label: "Registered office", lines: [], country: "", default: true }];
      addresses.forEach((address) => {
        addressList.appendChild(buildAddress(address, radioGroup));
      });
      ensureOneDefault(card);

      wireCard(card, radioGroup);
      return card;
    }

    // Show the entity id minimally. NEW entity: reveal the editable id field (the id
    // is the permanent key, set once at creation) and hide the caption. EXISTING
    // entity: hide the editable field, show a small de-emphasised caption with the id.
    function applyIdSurface(card, isNew) {
      const idField = card.querySelector("[data-entity-new-id-field]");
      const caption = field(card, "id-caption");
      const idValue = String(field(card, "id").value || "");
      if (isNew) {
        if (idField) idField.hidden = false;
        if (caption) {
          caption.hidden = true;
          caption.textContent = "";
        }
      } else {
        if (idField) idField.hidden = true;
        if (caption) {
          caption.hidden = !idValue;
          caption.textContent = idValue ? `id: ${idValue}` : "";
        }
      }
    }

    function buildAddress(address, radioGroup) {
      const fragment = addressTemplate.content.cloneNode(true);
      const row = fragment.querySelector("[data-entity-address]");
      addrField(row, "id").value = String(address.id || "");
      addrField(row, "label").value = String(address.label || "");
      addrField(row, "country").value = String(address.country || "");
      const lines = Array.isArray(address.lines) ? address.lines : [];
      const linesArea = addrField(row, "lines");
      linesArea.value = lines.join("\n");
      autoGrow(linesArea);
      linesArea.addEventListener("input", () => autoGrow(linesArea));
      const defaultRadio = addrField(row, "default");
      defaultRadio.name = radioGroup;
      defaultRadio.checked = Boolean(address.default);
      return row;
    }

    // Grow a textarea to fit its content (with a small floor) so addresses stay
    // compact when short but never clip when long -- replaces the giant fixed box.
    function autoGrow(textarea) {
      if (!textarea || typeof textarea.scrollHeight !== "number") return;
      textarea.style.height = "auto";
      const next = Math.max(textarea.scrollHeight, 38);
      textarea.style.height = `${next}px`;
    }

    function populateLawSelect(select, currentId) {
      select.innerHTML = "";
      const options = lawOptions.slice();
      // If the entity points at a law not in the playbook (an orphan), keep the
      // stale id selectable so the admin can SEE it (and the warning) rather than
      // it silently snapping to another law.
      if (currentId && !options.some((o) => o.id === currentId)) {
        options.unshift({ id: currentId, label: `${currentId} (not in playbook)` });
      }
      if (!options.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No playbook law options";
        select.appendChild(opt);
        return;
      }
      options.forEach((option) => {
        const opt = document.createElement("option");
        opt.value = option.id;
        opt.textContent = option.label || option.id;
        if (option.id === currentId) opt.selected = true;
        select.appendChild(opt);
      });
    }

    // ORPHAN GUARD (frontend): show an inline warning when the selected law is not
    // a current playbook option. The backend enforces this too (the save is
    // rejected); this is the early, in-editor signal.
    function updateLawWarning(card, lawId) {
      const warning = field(card, "law-warning");
      if (!warning) return;
      const known = lawOptions.some((o) => o.id === lawId);
      if (lawId && !known && playbookAvailable) {
        warning.hidden = false;
        warning.textContent = `Governing law "${lawId}" is not an approved playbook option. Pick an approved law before saving.`;
      } else {
        warning.hidden = true;
        warning.textContent = "";
      }
    }

    function wireCard(card, radioGroup) {
      // Any edit marks the registry dirty.
      card.addEventListener("input", () => setDirty(true));
      card.addEventListener("change", () => setDirty(true));

      const lawSelect = field(card, "governing_law");
      lawSelect.addEventListener("change", () => {
        updateLawWarning(card, lawSelect.value);
      });
      field(card, "legal_name").addEventListener("input", (event) => {
        field(card, "legal_name-display").textContent =
          event.target.value || "New entity";
      });

      card.querySelector("[data-entity-remove]")?.addEventListener("click", () => {
        // Confirm before deleting an already-persisted entity (the old behaviour
        // removed it instantly with no undo). A new, unsaved card removes silently.
        const isNew = card.dataset.entityNew === "true";
        if (!isNew) {
          const label =
            field(card, "legal_name").value.trim() ||
            field(card, "id").value.trim() ||
            "this entity";
          const confirmFn = typeof window !== "undefined" && typeof window.confirm === "function" ? window.confirm : null;
          if (confirmFn && !confirmFn(`Remove ${label}? This is saved only when you click Save registry.`)) {
            return;
          }
        }
        card.remove();
        setDirty(true);
        renumberMessageCount();
      });
      card.querySelector("[data-entity-address-add]")?.addEventListener("click", () => {
        const addressList = field(card, "address-list");
        addressList.appendChild(
          buildAddress({ id: "", label: "", lines: [], country: "", default: false }, radioGroup),
        );
        setDirty(true);
      });
      // Event-delegated address removal (rows are added dynamically).
      field(card, "address-list").addEventListener("click", (event) => {
        const removeButton = event.target.closest("[data-entity-address-remove]");
        if (!removeButton) return;
        const row = removeButton.closest("[data-entity-address]");
        if (row) {
          row.remove();
          ensureOneDefault(card);
          setDirty(true);
        }
      });
    }

    // Guarantee exactly one default address radio is checked within a card (the
    // backend requires exactly one). If none is checked, check the first.
    function ensureOneDefault(card) {
      const radios = card.querySelectorAll('[data-address-field="default"]');
      if (!radios.length) return;
      const anyChecked = Array.from(radios).some((r) => r.checked);
      if (!anyChecked) radios[0].checked = true;
    }

    function renumberMessageCount() {
      if (!list) return;
      const count = list.querySelectorAll("[data-entity-card]").length;
      setMessage(`${count} signing ${count === 1 ? "entity" : "entities"}.`);
    }

    function addEntity() {
      if (!list || !loaded) return;
      const card = buildCard(
        {
          id: "",
          legal_name: "",
          short_name: "",
          addresses: [{ id: "registered", label: "Registered office", lines: [], country: "", default: true }],
          governing_law: { playbook_option_id: lawOptions[0]?.id || "" },
          jurisdiction: "",
          incorporation_jurisdiction: "",
          signatory: { name: "[Authorised Signatory]", title: "[Title]" },
        },
        { isNew: true },
      );
      list.appendChild(card);
      setDirty(true);
      // Focus the legal name (the identity heading the redesign leads with); the
      // permanent entity-id field is revealed just below for the new entity.
      card.querySelector('[data-entity-field="legal_name"]')?.focus();
    }

    // Read the DOM back into the wire shape the backend expects.
    function collectEntities() {
      if (!list) return [];
      return Array.from(list.querySelectorAll("[data-entity-card]")).map((card) => {
        const lawId = field(card, "governing_law").value;
        const lawLabel =
          lawOptions.find((o) => o.id === lawId)?.label || lawId;
        const addresses = Array.from(
          field(card, "address-list").querySelectorAll("[data-entity-address]"),
        ).map((row) => ({
          id: addrField(row, "id").value.trim(),
          label: addrField(row, "label").value.trim(),
          lines: addrField(row, "lines")
            .value.split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
          country: addrField(row, "country").value.trim(),
          default: addrField(row, "default").checked,
        }));
        return {
          id: field(card, "id").value.trim(),
          legal_name: field(card, "legal_name").value.trim(),
          short_name: field(card, "short_name").value.trim(),
          jurisdiction: field(card, "jurisdiction").value.trim(),
          incorporation_jurisdiction: field(card, "incorporation_jurisdiction").value.trim(),
          governing_law: { playbook_option_id: lawId, label: lawLabel },
          signatory: {
            name: field(card, "signatory_name").value.trim(),
            title: field(card, "signatory_title").value.trim(),
          },
          addresses,
        };
      });
    }

    async function save() {
      if (!loaded) return;
      const entities = collectEntities();
      setControlsDisabled(true);
      setMessage("Saving registry...");
      try {
        const response = await fetch("/api/admin/signing-entities", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entities }),
        });
        const payload = await parseOk(response, "Registry could not be saved");
        applyWorkspace(payload);
      } catch (error) {
        // Re-enable controls so the admin can fix and retry; surface the reason.
        setControlsDisabled(false);
        setMessage(error.message || "Registry could not be saved", "error");
      }
    }

    function field(scope, name) {
      return scope.querySelector(`[data-entity-field="${name}"]`);
    }
    function addrField(scope, name) {
      return scope.querySelector(`[data-address-field="${name}"]`);
    }

    return { load };
  }

  return { createController };
})();

function createAdminEntitiesController(options) {
  return AdminEntitiesView.createController(options);
}

// CommonJS export for the Node test harness (a no-op in the browser).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AdminEntitiesView, createAdminEntitiesController };
}
