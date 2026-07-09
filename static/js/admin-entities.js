// Entities console: author the signing-entity registry (legal name, addresses,
// governing law joined to the playbook's approved options, court/jurisdiction,
// incorporation, signatory). Mirrors admin-access.js -- a GET probe on load (a
// 403 = "not an admin", rendered as a calm read-only state), an in-memory
// working copy edited card-by-card, and a publish-style Save that POSTs the full
// replacement registry.
//
// GOVERNING-LAW/COURT: an entity's governing law is a CONSTRAINED pick of the
// playbook's approved options (an editable <select> on every card) and its
// court a free-text input, with the same law->court auto-suggest coupling as
// the Playbook editor's "Entities & Courts" table. That table remains a second
// edit surface for the same fields -- both go through the same endpoint and the
// shared entityLawCourtWire helper, so they cannot drift on shape. The
// single-source-of-truth invariant is "law must be an approved playbook option"
// (enforced by the select + backend validation), NOT "only one screen may edit
// it".
const AdminEntitiesView = (() => {
  // Shared single-entity law/court wire-shape builder. Reused by BOTH this
  // console's collectEntities AND the Playbook "Entities & Courts" table, so the
  // two editors can never drift on the {governing_law, jurisdiction} contract.
  function entityLawCourtWire(lawId, lawLabel, jurisdiction) {
    return {
      governing_law: {
        playbook_option_id: String(lawId == null ? "" : lawId),
        label: String(lawLabel == null ? lawId || "" : lawLabel),
      },
      jurisdiction: String(jurisdiction == null ? "" : jurisdiction).trim(),
    };
  }

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
    // Optional transient SUCCESS-toast sink. Injected by app.js as
    // notificationsController.notifySuccess so a finished save flashes a green
    // toast through the ONE in-app notification center (no second toaster).
    // Absent in the Node test harness -> the call is guarded.
    notifySuccess,
  }) {
    // The in-memory working copy. `entities` is an array of plain objects the
    // user edits; `lawOptions` is [{id,label}] sourced from the playbook.
    let lawOptions = [];
    let playbookAvailable = false;
    let dirty = false;
    let loaded = false;
    // Optimistic-concurrency token from the last load; echoed on save so a stale
    // snapshot (another editor saved in between) is rejected (409) instead of
    // silently clobbering the other change.
    let currentEtag = "";
    // True only while a POST is in flight ("Saving registry..."). The Save button
    // is greyed out during this window and in the not-loaded/read-only case; at
    // every other rest it is a ready PURPLE CTA (a no-pending-changes click is a
    // harmless no-op re-save), so a finished save never leaves it disabled-grey.
    let saving = false;
    // The Save button's at-rest label, captured once so the dirty-state
    // "Save changes" relabel (syncSaveButton) can revert to it exactly.
    const saveButtonRestLabel = (saveButton && saveButton.textContent) || "Save registry";

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
      syncSaveButton();
    }

    // SINGLE source of truth for the Save button's enabled/disabled state. The
    // button reads as a ready PURPLE CTA whenever the registry is loaded + editable
    // (whether or not there are pending edits). It is greyed out ONLY while a save
    // is in flight, or when the registry is not loaded (read-only / error state).
    // This is what lets the button return to regular purple after a successful save
    // instead of going disabled-grey via the old dirty-gate.
    function syncSaveButton() {
      if (!saveButton) return;
      saveButton.disabled = saving || !loaded;
      // Unsaved-changes AFFORDANCE only (never gates enabled/disabled): pending
      // edits relabel the CTA "Save changes" + mark it with a class (styled in
      // styles.css); both revert once the registry is clean again (successful
      // save or reload).
      const showDirty = dirty && loaded && !saving;
      saveButton.classList.toggle("has-unsaved-changes", showDirty);
      saveButton.textContent = showDirty ? "Save changes" : saveButtonRestLabel;
    }

    function setControlsDisabled(disabled) {
      if (addButton) addButton.disabled = disabled;
      syncSaveButton();
    }

    // DISPLAY-ONLY friendly name for a governing-law option id. Prefers the
    // playbook's own label when the id is known; otherwise humanises the raw id
    // (e.g. "england_and_wales" -> "England And Wales"). Never used as a value.
    function humanizeLawId(lawId) {
      const id = String(lawId == null ? "" : lawId);
      const known = lawOptions.find((option) => option.id === id);
      if (known && known.label) return known.label;
      if (typeof window !== "undefined" && typeof window.humanizeId === "function") {
        return window.humanizeId(id);
      }
      return id;
    }

    // Fill a card's governing-law <select> with the approved playbook options.
    // A card with NO stored law (a brand-new entity) starts on a disabled
    // "Select governing law…" placeholder so an explicit pick is required (no
    // silent defaulting to the first option). A card whose stored law is no
    // longer an approved option (orphan) shows that value as a disabled
    // selected entry so the admin SEES it and must re-pick an approved law
    // (updateLawWarning flags it too).
    function populateLawSelect(select, currentLaw) {
      if (!select) return;
      select.innerHTML = "";
      const doc = select.ownerDocument;
      const addOption = (value, label, { disabled = false, selected = false } = {}) => {
        const option = doc.createElement("option");
        option.value = value;
        option.textContent = label;
        if (disabled) option.disabled = true;
        if (selected) option.selected = true;
        select.appendChild(option);
      };
      const known = lawOptions.some((o) => o.id === currentLaw);
      if (!currentLaw) {
        addOption("", "Select governing law…", { disabled: true, selected: true });
      } else if (!known) {
        addOption(currentLaw, `${humanizeLawId(currentLaw)} (not an approved option)`, {
          disabled: true,
          selected: true,
        });
      }
      lawOptions.forEach((option) => {
        addOption(option.id, option.label || option.id, { selected: option.id === currentLaw });
      });
    }

    // The canonical court a given law option expects, and the option's
    // jurisdiction key. MINIMAL duplicates of suggestedCourtForLaw()/
    // lawForumKey() in playbook-view.js's "Entities & Courts" table (the source
    // of this law->court coupling behaviour) -- both surfaces read the same
    // court_name / forum_jurisdiction fields off the same
    // /api/admin/signing-entities governing_law_options payload.
    function suggestedCourtForLaw(lawId) {
      const opt = lawOptions.find((o) => o.id === lawId);
      if (!opt) return "";
      const courtName = String(opt.court_name || "").trim();
      if (courtName) return courtName;
      const forum = String(opt.forum_jurisdiction || "").trim();
      return forum ? `courts in ${forum}` : "";
    }

    function lawForumKey(lawId) {
      const opt = lawOptions.find((o) => o.id === lawId);
      return opt ? String(opt.forum_jurisdiction || opt.id || "").trim().toLowerCase() : "";
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
      // A LOAD failure leaves nothing editable: drop out of the loaded state so the
      // Save button greys out via the shared gate, and keep the error visible.
      loaded = false;
      if (addButton) addButton.disabled = true;
      syncSaveButton();
      setMessage(text, "error");
    }

    function applyWorkspace(payload = {}) {
      loaded = true;
      lawOptions = Array.isArray(payload.governing_law_options)
        ? payload.governing_law_options.filter((o) => o && o.id)
        : [];
      playbookAvailable = Boolean(payload.playbook_available);
      if (typeof payload.etag === "string") currentEtag = payload.etag;
      const entities = Array.isArray(payload.entities) ? payload.entities : [];
      renderList(entities);
      if (addButton) addButton.disabled = false;
      setDirty(false);
      if (payload.saved) {
        // SUCCESS feedback is now a transient green toast (fired from save()), not
        // lingering inline green text. Settle the inline message back to the neutral
        // resting state (the entity count) so nothing green lingers under the heading.
        setMessage(`${entities.length} signing ${entities.length === 1 ? "entity" : "entities"}.`);
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
      // autoGrow at build time can see scrollHeight 0 (the textarea was not yet
      // attached/laid out), so re-size every lines box once the cards are in the
      // DOM, on the next frame when layout is settled.
      growAllAddressLines();
    }

    function growAllAddressLines() {
      if (!list) return;
      const run = () => {
        list.querySelectorAll('[data-address-field="lines"]').forEach((area) => autoGrow(area));
      };
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(run);
      } else {
        run();
      }
    }

    function buildCard(entity, { isNew = false } = {}) {
      const fragment = cardTemplate.content.cloneNode(true);
      const card = fragment.querySelector("[data-entity-card]");
      const radioGroup = `entity-default-${(radioGroupSeq += 1)}`;

      // The entity id is the persistent key, SYSTEM-ASSIGNED by the backend from
      // the legal name on first save. It is never rendered or editable anywhere —
      // it lives only in the hidden input that save reads: an existing entity
      // round-trips its key, a new card posts "" and the backend fills it in.
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

      // Governing law: an editable <select> constrained to the playbook's
      // approved options. Court: an editable text input (already filled from
      // entity.jurisdiction above). Both are edited directly on the card; the
      // Playbook "Entities & Courts" table remains a second surface for the
      // same fields via the same wire helper.
      const currentLaw = String(entity.governing_law?.playbook_option_id || "");
      populateLawSelect(field(card, "governing_law"), currentLaw);
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
      // Floor kept in LOCKSTEP with .entity-address-lines { min-height: 86px }
      // in styles.css -- change both together.
      const next = Math.max(textarea.scrollHeight, 86);
      textarea.style.height = `${next}px`;
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
        warning.textContent = `Governing law "${humanizeLawId(lawId)}" is not an approved playbook option. Pick an approved law before saving.`;
      } else {
        warning.hidden = true;
        warning.textContent = "";
      }
    }

    function wireCard(card, radioGroup) {
      // Any edit marks the registry dirty.
      card.addEventListener("input", () => setDirty(true));
      card.addEventListener("change", () => setDirty(true));

      // Law <-> court COUPLING (mirrors the Playbook "Entities & Courts" table,
      // playbook-view.js render()): when a law change moves the entity to a
      // DIFFERENT jurisdiction and the current court doesn't already match it,
      // re-suggest the matching court so a lone law change can't trip the
      // backend forum-reconciliation guard (HTTP 400 "forum drift") on save. An
      // inline note says the court was updated so nothing happens silently; a
      // deliberately-specific in-jurisdiction court is preserved.
      const lawSelect = field(card, "governing_law");
      if (lawSelect) {
        lawSelect.dataset.prevLaw = lawSelect.value;
        lawSelect.addEventListener("change", () => {
          const newLawId = lawSelect.value;
          const prevLawId = lawSelect.dataset.prevLaw || "";
          lawSelect.dataset.prevLaw = newLawId;
          updateLawWarning(card, newLawId);
          const courtInput = field(card, "jurisdiction");
          const note = field(card, "court-note");
          if (!courtInput) return;
          const jurisdictionChanged = lawForumKey(newLawId) !== lawForumKey(prevLawId);
          const suggested = suggestedCourtForLaw(newLawId);
          if (!suggested) return;
          // Already matches the new law's jurisdiction phrase -> leave the court.
          const currentCourt = String(courtInput.value || "").trim().toLowerCase();
          const forumKey = lawForumKey(newLawId);
          const alreadyMatches = forumKey && currentCourt.includes(forumKey);
          if (jurisdictionChanged && !alreadyMatches) {
            courtInput.value = suggested;
            if (note) {
              note.textContent = `Court updated to “${suggested}” to match the new governing law. Edit it if a more specific court applies.`;
              note.hidden = false;
            }
          }
        });
      }

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
        const row = buildAddress(
          { id: "", label: "", lines: [], country: "", default: false },
          radioGroup,
        );
        addressList.appendChild(row);
        setDirty(true);
        // Mirror addEntity()'s focus handoff: drop the caret straight into the
        // new row's Label so the admin can start typing immediately.
        row.querySelector('[data-address-field="label"]')?.focus();
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
          // NO default law: the select starts on the disabled "Select governing
          // law…" placeholder so the admin must pick explicitly (defaulting to
          // lawOptions[0] silently made every new entity Indian-law).
          governing_law: { playbook_option_id: "" },
          jurisdiction: "",
          incorporation_jurisdiction: "",
          signatory: { name: "[Authorised Signatory]", title: "[Title]" },
        },
        { isNew: true },
      );
      list.appendChild(card);
      setDirty(true);
      // Focus the legal name (the identity heading the redesign leads with). There
      // is no id field to fill: the backend assigns the permanent id from the
      // legal name when the new entity is first saved.
      card.querySelector('[data-entity-field="legal_name"]')?.focus();
    }

    // Read the DOM back into the wire shape the backend expects.
    function collectEntities() {
      if (!list) return [];
      return Array.from(list.querySelectorAll("[data-entity-card]")).map((card) => {
        const lawId = field(card, "governing_law").value;
        const lawLabel =
          lawOptions.find((o) => o.id === lawId)?.label || lawId;
        const lawCourt = entityLawCourtWire(
          lawId,
          lawLabel,
          field(card, "jurisdiction").value,
        );
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
          jurisdiction: lawCourt.jurisdiction,
          incorporation_jurisdiction: field(card, "incorporation_jurisdiction").value.trim(),
          governing_law: lawCourt.governing_law,
          signatory: {
            name: field(card, "signatory_name").value.trim(),
            title: field(card, "signatory_title").value.trim(),
          },
          addresses,
        };
      });
    }

    async function save() {
      if (!loaded || saving) return;
      // An unpicked governing law (the placeholder) would 400 on the backend
      // anyway ("law must be an approved playbook option"); surface it as a
      // clear inline message instead of a server round-trip.
      const missingLaw = Array.from(
        list ? list.querySelectorAll("[data-entity-card]") : [],
      ).find((card) => !field(card, "governing_law").value);
      if (missingLaw) {
        const label =
          field(missingLaw, "legal_name").value.trim() ||
          field(missingLaw, "id").value.trim() ||
          "the new entity";
        setMessage(`Pick a governing law for ${label} before saving.`, "error");
        return;
      }
      const entities = collectEntities();
      // In-flight: grey out the Save button (and Add) for the "Saving registry..."
      // moment only. saving=true is the single condition syncSaveButton() reads.
      saving = true;
      setControlsDisabled(true);
      setMessage("Saving registry...");
      try {
        const response = await fetch("/api/admin/signing-entities", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // Echo the etag from the last load so a stale snapshot (another editor
          // saved in between) is rejected with a 409 rather than clobbering them.
          body: JSON.stringify({ entities, etag: currentEtag }),
        });
        const payload = await parseOk(response, "Registry could not be saved");
        // Save persisted: clear the in-flight flag BEFORE applyWorkspace so the Save
        // button settles back to its ready PURPLE state (not disabled-grey), then
        // flash a transient green success toast through the shared notification
        // center. The inline message settles to the neutral entity count.
        saving = false;
        applyWorkspace(payload);
        const saved = Array.isArray(payload.entities) ? payload.entities.length : entities.length;
        if (typeof notifySuccess === "function") {
          notifySuccess(
            "Registry saved",
            `${saved} signing ${saved === 1 ? "entity" : "entities"} saved`,
          );
        }
      } catch (error) {
        // A save FAILURE stays visible: re-enable controls so the admin can fix and
        // retry, and surface the reason as a PERSISTENT inline error (never a
        // transient toast that would vanish before it is read).
        saving = false;
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

  return { createController, entityLawCourtWire };
})();

function createAdminEntitiesController(options) {
  return AdminEntitiesView.createController(options);
}

// CommonJS export for the Node test harness (a no-op in the browser).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    AdminEntitiesView,
    createAdminEntitiesController,
    entityLawCourtWire: AdminEntitiesView.entityLawCourtWire,
  };
}
