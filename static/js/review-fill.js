// Inbound NDA "Fill" tool — the 3rd Review-workstation inspector tab.
//
// An inbound NDA usually arrives with blanks: underscore runs, [bracketed
// tokens], dotted lines, and empty "Label:" slots. This tool lets the reviewer
// pick one of our Aspora signing entities (and, for the multi-address entity,
// which address signs), then fill each detected blank with the matching entity
// value — choosing per-blank whether the fill goes in CLEAN (rewriting the
// paragraph text so the viewer shows the filled NDA) or TRACKED (left for the
// backend to render as a tracked change on export).
//
// It mirrors the createContractStructureController pattern: a factory returning
// { render() } that paints into #studioDetailPanel when the Fill tab is active.
// All entity data comes from the same registry the generator uses — the bridged
// window.createDraftIntake helper surface (which reads /api/signing-entities via
// the controller, with an embedded mirror fallback). Here we feed it the live
// feed when available and otherwise fall back to the embedded SIGNING_ENTITIES.
//
// Pure DOM + state; no framework. escapeHtml is resolved lazily through
// window.escapeHtml (bridged by global-bridge.mjs) since this classic script's
// render only runs at interaction time, never at load time.

function createFillController({ state, root, rerenderDocument }) {
  // The entity helper surface (window.createDraftIntake). Bound lazily so the
  // deferred bridge module has loaded by first render. Re-bound once the live
  // /api/signing-entities feed resolves so the picker reflects the deployed
  // bundles; until then it runs on the embedded mirror.
  let entityApi = null;
  let registryLoaded = false;
  let registryLoading = false;

  // The current picker selection. Reuses the intake shape so we can lean on the
  // same coupled entity+address+law helpers the generator uses.
  let pick = null;

  // Per-blank working state, keyed by blank id: { value, field, mode, enabled }.
  // Survives re-renders so the user's overrides/toggles aren't lost when the
  // panel repaints (e.g. after picking an entity).
  const blankState = new Map();

  const FIELD_OPTIONS = [
    { id: "legal_name", label: "Entity legal name" },
    { id: "registered_office", label: "Registered office" },
    { id: "incorporation_jurisdiction", label: "Incorporation jurisdiction" },
    { id: "governing_law", label: "Governing law" },
    { id: "signatory_name", label: "Signatory name" },
    { id: "signatory_title", label: "Signatory title" },
    { id: "custom", label: "Custom value" },
  ];

  function escape(value) {
    return typeof window !== "undefined" && typeof window.escapeHtml === "function"
      ? window.escapeHtml(value)
      : String(value == null ? "" : value);
  }

  function api() {
    if (!entityApi) {
      entityApi = window.createDraftIntake({});
      pick = entityApi.createInitialIntake();
    }
    return entityApi;
  }

  // Loads the live signing-entity feed once, then re-binds the helper surface to
  // it (preserving the in-progress picker selection). A 404 / network error
  // leaves the embedded mirror in place — the picker stays fully usable.
  function ensureRegistry() {
    if (registryLoaded || registryLoading) return;
    registryLoading = true;
    fetch("/api/signing-entities", { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (Array.isArray(payload?.entities) && payload.entities.length) {
          const previous = pick;
          entityApi = window.createDraftIntake({ entities: payload.entities });
          pick = entityApi.createInitialIntake();
          if (previous?.entityId) {
            pick = entityApi.applyEntitySelection(pick, previous.entityId);
            if (previous.addressId) pick = entityApi.selectAddress(pick, previous.addressId);
            // Repaint so the newly-loaded entity labels/values appear.
            if (state.reviewInspectorView === "fill") render();
          }
        }
      })
      .catch(() => {
        // Stay on the embedded mirror.
      })
      .finally(() => {
        registryLoaded = true;
        registryLoading = false;
      });
  }

  // ── Candidate entity values ───────────────────────────────────────────────
  // Derives the fillable values from the current entity + address selection.
  function entityValues() {
    api();
    const entity = entityApi.selectedEntity(pick);
    if (!entity) return null;
    const address = entityApi.selectedAddress(pick);
    const law = entityApi.effectiveGoverningLaw(pick);
    const signatory = (entity.signatory && typeof entity.signatory === "object") ? entity.signatory : {};
    return {
      legal_name: String(entity.legal_name || "").trim(),
      registered_office: entityApi.formatAddressLines(address),
      incorporation_jurisdiction: String(
        entity.incorporation_jurisdiction || entity.jurisdiction || "",
      ).trim(),
      governing_law: String(law?.label || "").trim(),
      signatory_name: String(signatory.name || "").trim(),
      signatory_title: String(signatory.title || "").trim(),
    };
  }

  function valueForField(field) {
    const values = entityValues();
    if (!values || field === "custom") return "";
    return values[field] || "";
  }

  // ── Blank detection ───────────────────────────────────────────────────────
  // Scans every loaded paragraph for blank tokens and proposes a field per blank
  // from nearby label keywords. Returns an ordered list of detected blanks.
  function detectBlanks() {
    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    const blanks = [];
    paragraphs.forEach((paragraph) => {
      const text = String(paragraph?.text || "");
      if (!text.trim()) return;
      const seen = new Set();
      blankMatches(text).forEach((match) => {
        const find = match.text;
        if (!find || seen.has(match.index)) return;
        seen.add(match.index);
        const field = suggestField(text, match.index, find);
        blanks.push({
          id: `fill-${paragraph.id}-${match.index}`,
          paragraph_id: String(paragraph.id),
          paragraph_index: paragraph.index ?? paragraph.source_index ?? null,
          context: text,
          find,
          offset: match.index,
          field,
        });
      });
    });
    return blanks;
  }

  // The blank patterns: underscore runs, [bracketed tokens], dotted/ellipsis
  // lines. Returns {text, index} for each, sorted by position so the per-blank
  // rows read top-to-bottom through the paragraph.
  function blankMatches(text) {
    const patterns = [
      /_{3,}/g, // underscore runs
      /\[[^\]]*\]/g, // [bracketed tokens]
      /\.{4,}|…+/g, // dotted lines / ellipses
    ];
    const matches = [];
    patterns.forEach((pattern) => {
      let match = pattern.exec(text);
      while (match) {
        matches.push({ text: match[0], index: match.index });
        // Guard against zero-length matches looping forever.
        if (match.index === pattern.lastIndex) pattern.lastIndex += 1;
        match = pattern.exec(text);
      }
    });
    return matches.sort((a, b) => a.index - b.index);
  }

  // Suggests a field for a blank from label keywords in the ~60 chars before it
  // (and a little after, to catch "____ (Authorised Signatory)" style trailers).
  // "Label: ___" and bare "Label:" both work because the label text precedes the
  // blank. Falls back to "custom" when no keyword matches.
  function suggestField(text, index, find) {
    const before = text.slice(Math.max(0, index - 80), index).toLowerCase();
    const after = text.slice(index + find.length, index + find.length + 40).toLowerCase();
    const window = `${before} ${after}`;
    // Order matters: more specific phrases first so "registered office" wins over
    // a bare "office", and "governing law"/"jurisdiction" beat a generic match.
    if (/governing law|laws of/.test(window)) return "governing_law";
    if (/registered office|address|registered at|having its office|principal place/.test(window)) {
      return "registered_office";
    }
    if (/incorporat|jurisdiction|organized under|organised under/.test(window)) {
      return "incorporation_jurisdiction";
    }
    if (/authorised signatory|authorized signatory|signatory|signed by|signature/.test(window)) {
      return "signatory_name";
    }
    if (/title|designation|position/.test(window)) return "signatory_title";
    if (/company name|legal name|name of (?:the )?(?:company|party|entity)|\bname\b|company|party|entity/.test(window)) {
      return "legal_name";
    }
    return "custom";
  }

  // Working state for a blank, seeded from the suggested field's entity value.
  // Re-seeds the value only while the user hasn't typed a custom one (tracked by
  // `dirty`), so changing the picked entity refreshes the prefill but a manual
  // override is preserved.
  function workingFor(blank) {
    const existing = blankState.get(blank.id);
    if (existing) {
      if (!existing.dirty) {
        const seeded = valueForField(existing.field);
        if (seeded) existing.value = seeded;
      }
      return existing;
    }
    const fresh = {
      field: blank.field,
      value: valueForField(blank.field),
      mode: "clean",
      enabled: true,
      dirty: false,
    };
    blankState.set(blank.id, fresh);
    return fresh;
  }

  // ── Render ────────────────────────────────────────────────────────────────
  function render() {
    if (!root) return;
    ensureRegistry();
    api();

    // Gate: generated NDAs were drafted by us with no blanks to fill.
    if (state.selectedMatter?.source_type === "generated") {
      root.innerHTML = `
        <div class="fill-empty">No blanks to fill — this NDA was generated by Aspora.</div>
      `;
      return;
    }

    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    if (!paragraphs.length) {
      root.innerHTML = `
        <div class="fill-empty">Load or review an inbound NDA to scan it for blanks to fill.</div>
      `;
      return;
    }

    const blanks = detectBlanks();
    root.innerHTML = `
      ${renderEntityPicker()}
      ${renderAppliedSummary()}
      ${blanks.length ? renderBlanksList(blanks) : '<div class="fill-empty">No blanks detected in this document.</div>'}
      ${blanks.length ? renderActions() : ""}
    `;
    bindControls(blanks);
  }

  function renderEntityPicker() {
    api();
    const entity = entityApi.selectedEntity(pick);
    const entityOptions = [
      '<option value="">Choose entity…</option>',
      ...entityApi.entities.map((item) => `<option value="${escape(item.id)}"${pick.entityId === item.id ? " selected" : ""}>${escape(entityApi.entityLabel(item))}</option>`),
    ].join("");

    let addressField = "";
    if (entity && entityApi.hasMultipleAddresses(entity)) {
      const addressOptions = entity.addresses.map((address) => `<option value="${escape(address.id)}"${pick.addressId === address.id ? " selected" : ""}>${escape(`${address.label} — ${entityApi.formatAddressLines(address)}`)}</option>`).join("");
      addressField = `
        <label class="fill-field">
          <span>Address</span>
          <select data-fill-address>${addressOptions}</select>
        </label>
      `;
    }

    const values = entityValues();
    const bundle = values
      ? `
        <dl class="fill-bundle-grid">
          <div><dt>Legal name</dt><dd>${escape(values.legal_name || "—")}</dd></div>
          <div><dt>Registered office</dt><dd>${escape(values.registered_office || "—")}</dd></div>
          <div><dt>Governing law</dt><dd>${escape(values.governing_law || "—")}</dd></div>
        </dl>
      `
      : '<p class="fill-bundle-empty">Pick an entity to source fill values from its bundle.</p>';

    return `
      <section class="fill-picker" aria-label="Aspora entity">
        <div class="fill-picker-fields">
          <label class="fill-field">
            <span>Aspora entity</span>
            <select data-fill-entity>${entityOptions}</select>
          </label>
          ${addressField}
        </div>
        ${bundle}
      </section>
    `;
  }

  function renderBlanksList(blanks) {
    return `
      <section class="fill-blank-list" aria-label="Detected blanks">
        ${blanks.map(renderBlankRow).join("")}
      </section>
    `;
  }

  function renderBlankRow(blank) {
    const work = workingFor(blank);
    const fieldOptions = FIELD_OPTIONS.map((option) => `<option value="${escape(option.id)}"${work.field === option.id ? " selected" : ""}>${escape(option.label)}</option>`).join("");
    const contextHtml = renderContext(blank);
    const paragraphLabel = blank.paragraph_index != null ? `Paragraph ${escape(blank.paragraph_index)}` : "Paragraph";
    const tracked = work.mode === "tracked";
    return `
      <article class="fill-blank-row${work.enabled ? "" : " disabled"}" data-fill-blank-id="${escape(blank.id)}">
        <header class="fill-blank-head">
          <label class="fill-blank-enable">
            <input type="checkbox" data-fill-enable${work.enabled ? " checked" : ""}>
            <span>${paragraphLabel}</span>
          </label>
          <code class="fill-blank-token">${escape(blank.find)}</code>
        </header>
        <p class="fill-blank-context">${contextHtml}</p>
        <div class="fill-blank-controls">
          <label class="fill-field">
            <span>Field</span>
            <select data-fill-field>${fieldOptions}</select>
          </label>
          <label class="fill-field fill-field-value">
            <span>Value</span>
            <input type="text" data-fill-value value="${escape(work.value)}" placeholder="Value to insert">
          </label>
          <div class="fill-mode-toggle" role="group" aria-label="Fill mode">
            <button type="button" data-fill-mode="clean" class="${tracked ? "" : "active"}" aria-pressed="${tracked ? "false" : "true"}">Clean</button>
            <button type="button" data-fill-mode="tracked" class="${tracked ? "active" : ""}" aria-pressed="${tracked ? "true" : "false"}">Tracked</button>
          </div>
        </div>
      </article>
    `;
  }

  // Renders the paragraph context with the blank token highlighted in place.
  function renderContext(blank) {
    const text = String(blank.context || "");
    const start = Number(blank.offset) || 0;
    const end = start + String(blank.find || "").length;
    const head = clip(text.slice(0, start), -90);
    const tail = clip(text.slice(end), 90);
    return `${escape(head)}<mark class="fill-blank-mark">${escape(blank.find)}</mark>${escape(tail)}`;
  }

  function clip(value, limit) {
    const text = String(value || "");
    if (limit < 0) {
      const max = -limit;
      return text.length <= max ? text : `…${text.slice(text.length - max)}`;
    }
    return text.length <= limit ? text : `${text.slice(0, limit)}…`;
  }

  function renderActions() {
    return `
      <div class="fill-actions">
        <button type="button" class="fill-apply" data-fill-apply>Apply fills</button>
      </div>
    `;
  }

  function renderAppliedSummary() {
    const fills = Array.isArray(state.filledBlanks) ? state.filledBlanks : [];
    if (!fills.length) return "";
    const cleanCount = fills.filter((fill) => fill.mode === "clean").length;
    const trackedCount = fills.length - cleanCount;
    return `
      <div class="fill-applied" role="status">
        ${escape(fills.length)} ${fills.length === 1 ? "fill" : "fills"} applied
        (${escape(cleanCount)} clean, ${escape(trackedCount)} tracked).
        <button type="button" class="fill-clear" data-fill-clear>Clear applied fills</button>
      </div>
    `;
  }

  // ── Events ────────────────────────────────────────────────────────────────
  function bindControls(blanks) {
    const entitySelect = root.querySelector("[data-fill-entity]");
    if (entitySelect) {
      entitySelect.addEventListener("change", () => {
        pick = entityApi.applyEntitySelection(pick, entitySelect.value);
        render();
      });
    }
    const addressSelect = root.querySelector("[data-fill-address]");
    if (addressSelect) {
      addressSelect.addEventListener("change", () => {
        pick = entityApi.selectAddress(pick, addressSelect.value);
        render();
      });
    }

    const blankById = new Map(blanks.map((blank) => [blank.id, blank]));
    root.querySelectorAll("[data-fill-blank-id]").forEach((rowNode) => {
      const blankId = rowNode.dataset.fillBlankId;
      const blank = blankById.get(blankId);
      if (!blank) return;
      const work = workingFor(blank);

      const enable = rowNode.querySelector("[data-fill-enable]");
      enable?.addEventListener("change", () => {
        work.enabled = enable.checked;
        rowNode.classList.toggle("disabled", !work.enabled);
      });

      const fieldSelect = rowNode.querySelector("[data-fill-field]");
      const valueInput = rowNode.querySelector("[data-fill-value]");
      fieldSelect?.addEventListener("change", () => {
        work.field = fieldSelect.value;
        // Re-seed the value from the newly-chosen field unless the user has typed
        // a custom value already.
        if (!work.dirty) {
          const seeded = valueForField(work.field);
          work.value = seeded;
          if (valueInput) valueInput.value = seeded;
        }
      });
      valueInput?.addEventListener("input", () => {
        work.value = valueInput.value;
        work.dirty = true;
      });

      rowNode.querySelectorAll("[data-fill-mode]").forEach((button) => {
        button.addEventListener("click", () => {
          work.mode = button.dataset.fillMode === "tracked" ? "tracked" : "clean";
          rowNode.querySelectorAll("[data-fill-mode]").forEach((other) => {
            const active = other === button;
            other.classList.toggle("active", active);
            other.setAttribute("aria-pressed", active ? "true" : "false");
          });
        });
      });
    });

    root.querySelector("[data-fill-apply]")?.addEventListener("click", () => applyFills(blanks));
    root.querySelector("[data-fill-clear]")?.addEventListener("click", () => clearFills());
  }

  // ── Apply ─────────────────────────────────────────────────────────────────
  // Commits the enabled blanks: pushes a record to state.filledBlanks and, for
  // CLEAN fills, rewrites the paragraph text + advances the manual-redline
  // baseline so manualExportRedlines() doesn't also emit a tracked redline for
  // the same change (avoiding double counting). TRACKED fills leave the text and
  // baseline untouched — the backend renders them as tracked changes on export.
  function applyFills(blanks) {
    api();
    let cleanTouched = false;
    let applied = 0;
    blanks.forEach((blank) => {
      const work = workingFor(blank);
      if (!work.enabled) return;
      const value = String(work.value || "").trim();
      if (!value) return;
      const record = {
        id: blank.id,
        paragraph_id: blank.paragraph_id,
        find: blank.find,
        value,
        field: work.field,
        mode: work.mode === "tracked" ? "tracked" : "clean",
      };
      upsertFill(record);
      applied += 1;
      if (record.mode === "clean") {
        if (applyCleanFill(record)) cleanTouched = true;
      }
    });

    if (cleanTouched && typeof rerenderDocument === "function") {
      rerenderDocument();
    }
    // Mark the matter's redline draft dirty so the applied fills are saved/sent
    // alongside the other review edits (no-op when there's no loaded matter).
    if (typeof markRedlineDraftDirty === "function") markRedlineDraftDirty();
    render();
    if (typeof setFileMeta === "function") {
      setFileMeta(applied
        ? `Applied ${applied} ${applied === 1 ? "fill" : "fills"} from the entity bundle.`
        : "No blanks were filled — enable a blank and give it a value.");
    }
  }

  // Rewrites the first occurrence of the blank token in the paragraph with the
  // value, in BOTH the live paragraph and the export baseline, so the viewer
  // shows the filled text and manualExportRedlines() sees no difference for it.
  // Returns true when it actually mutated text.
  function applyCleanFill(record) {
    let touched = false;
    const replaceIn = (list) => {
      if (!Array.isArray(list)) return;
      const paragraph = list.find((item) => String(item.id) === String(record.paragraph_id));
      if (!paragraph) return;
      const text = String(paragraph.text || "");
      const at = text.indexOf(record.find);
      if (at === -1) return;
      paragraph.text = text.slice(0, at) + record.value + text.slice(at + record.find.length);
      touched = true;
    };
    replaceIn(state.reviewParagraphs);
    // Advance BOTH baseline snapshots so the clean fill is treated as the new
    // original — manualRedlineBaselineParagraphs() prefers reviewExportOriginalParagraphs.
    replaceIn(state.reviewExportOriginalParagraphs);
    replaceIn(state.reviewOriginalParagraphs);
    return touched;
  }

  function upsertFill(record) {
    if (!Array.isArray(state.filledBlanks)) state.filledBlanks = [];
    state.filledBlanks = state.filledBlanks.filter((fill) => fill.id !== record.id);
    state.filledBlanks.push(record);
  }

  function clearFills() {
    state.filledBlanks = [];
    render();
    if (typeof markRedlineDraftDirty === "function") markRedlineDraftDirty();
    if (typeof setFileMeta === "function") setFileMeta("Cleared applied fills.");
  }

  return { render };
}

// Export payload helper, shared by the DOCX export (and any other outbound
// flow). Maps state.filledBlanks to the backend-agreed shape:
// { paragraph_id, find, value, mode }. A global (classic-script) function so the
// review-workstation action modules can call it without importing the controller.
function currentReviewFills() {
  const fills = Array.isArray(state.filledBlanks) ? state.filledBlanks : [];
  return fills.map((fill) => ({
    paragraph_id: fill.paragraph_id,
    find: fill.find,
    value: fill.value,
    mode: fill.mode === "tracked" ? "tracked" : "clean",
  }));
}
