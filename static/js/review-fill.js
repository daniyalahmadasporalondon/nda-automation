// NDA "Fill" tool — the 3rd Review-workstation inspector tab.
//
// Manages ASPORA'S IDENTITY (entity legal name + registered address) in an NDA,
// deterministically, in two modes:
//   1. INSERT  — find empty name/address blanks (underscore runs, [bracketed
//      tokens], dotted lines) and insert the chosen Aspora entity's legal name /
//      registered address.
//   2. REPLACE — find an Aspora entity's name/address ALREADY present in the
//      document (matched against our signing-entity registry) and swap it for the
//      chosen entity's name/address.
// Each item applies CLEAN (rewrite the paragraph text so the viewer shows it) or
// TRACKED (a tracked change the backend renders on export). Everything else
// (governing law, term, signatory names, ...) is handled by clause review — this
// tool is entity name + registered address ONLY.
//
// Factory returning { render() } that paints into #studioDetailPanel when the Fill
// tab is active. Entity data comes from the registry via the bridged
// window.createDraftIntake helper (live /api/signing-entities, embedded fallback).
// Pure DOM + state; escapeHtml is resolved lazily via window.escapeHtml.

function createFillController({ state, root, rerenderDocument }) {
  let entityApi = null;
  let registryLoaded = false;
  let registryLoading = false;

  // The chosen Aspora entity (+ address) to insert / replace with. Reuses the
  // generator's intake shape so we lean on the same entity/address helpers.
  let pick = null;

  // Per-candidate working state keyed by candidate id: { mode, enabled }. Survives
  // re-renders so toggles aren't lost when the panel repaints.
  const itemState = new Map();

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

  // Loads the live signing-entity feed once, then re-binds the helper to it
  // (preserving the in-progress selection). A 404/network error keeps the embedded
  // mirror — the picker stays usable.
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
          }
          if (state.reviewInspectorView === "fill") render();
        }
      })
      .catch(() => {})
      .finally(() => {
        registryLoaded = true;
        registryLoading = false;
      });
  }

  // ── Chosen entity values (name + registered address only) ───────────────────
  function targetEntity() {
    api();
    return entityApi.selectedEntity(pick);
  }
  function targetName() {
    const entity = targetEntity();
    return entity ? String(entity.legal_name || "").trim() : "";
  }
  function targetAddress() {
    api();
    if (!entityApi.selectedEntity(pick)) return "";
    return String(entityApi.formatAddressLines(entityApi.selectedAddress(pick)) || "").trim();
  }
  function valueForSlot(slot) {
    return slot === "address" ? targetAddress() : targetName();
  }
  function registryEntities() {
    api();
    return Array.isArray(entityApi.entities) ? entityApi.entities : [];
  }
  function entityAddressStrings(entity) {
    const addresses = Array.isArray(entity.addresses) ? entity.addresses : [];
    return addresses
      .map((address) => String(entityApi.formatAddressLines(address) || "").trim())
      .filter(Boolean);
  }

  // ── Detection: INSERT (empty entity-name / address blanks) ──────────────────
  const BLANK_PATTERNS = [/_{3,}/g, /\[[^\]]*\]/g, /\.{4,}|…+/g];

  function blankMatches(text) {
    const matches = [];
    BLANK_PATTERNS.forEach((pattern) => {
      pattern.lastIndex = 0;
      let match = pattern.exec(text);
      while (match) {
        matches.push({ text: match[0], index: match.index });
        if (match.index === pattern.lastIndex) pattern.lastIndex += 1;
        match = pattern.exec(text);
      }
    });
    return matches.sort((a, b) => a.index - b.index);
  }

  // Returns "name" | "address" | null from label keywords around the blank. Only
  // ENTITY-name slots qualify as "name"; a bare signatory "Name:" (no company/party
  // context) is skipped — that's clause review's job, not ours.
  function classifyBlank(text, index, find) {
    const beforeFull = text.slice(Math.max(0, index - 120), index).toLowerCase();
    // The immediate label leading into the blank — what tells us name vs address.
    const beforeClose = text.slice(Math.max(0, index - 48), index).toLowerCase();
    const after = text.slice(index + find.length, index + find.length + 40);
    // Signatory/person name lines are clause-review's job, not ours.
    const signatoryCtx = /for and on behalf|signature|signed by|authoris|authoriz|designation|witness/.test(beforeFull);

    // 1. A defined term right after the blank — e.g. ___ ("Company") — is a party NAME.
    if (/^\s*\(\s*["“']?(?:the\s+)?(?:company|recipient|disclosing|receiving|client|customer|vendor|supplier|party)/i.test(after)) {
      return signatoryCtx ? null : "name";
    }
    // 2. An address label leading INTO the blank — "...registered office at ___".
    // Require office/registered-office context, or a bare "address" that isn't an
    // EMAIL / web / IP address (those aren't a registered office to fill).
    const officeCtx = /registered office|having its (?:registered )?office|office (?:at|located)|principal (?:place|office)/.test(beforeClose);
    const plainAddress = /\baddress\b/.test(beforeClose) && !/e-?mail|electronic mail|\bmail\b|web ?site|\bweb\b|\bip\b/.test(beforeClose);
    if (officeCtx || plainAddress) {
      return "address";
    }
    // 3. A party-definition tail right after the blank — "___, a company incorporated
    // under...", "___, a small finance bank...", "___, an entity organized under..." —
    // marks the blank as the party NAME. The defined term like ("Company") often
    // appears much later in the sentence, not immediately after the blank.
    if (/^\s*[,(]?\s*(?:a|an)\s+[\w.&'’\/ -]{0,45}?\b(?:company|corporation|limited|ltd|llc|inc|bank|firm|partnership|entity|llp|society|trust|incorporated|organi[sz]ed|registered under)\b/i.test(after)) {
      return signatoryCtx ? null : "name";
    }
    // 4. An explicit entity-name label leading into the blank.
    if (/company name|legal name|name of (?:the )?(?:company|party|entity)/.test(beforeClose)) {
      return signatoryCtx ? null : "name";
    }
    return null;
  }

  function detectInserts() {
    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    const out = [];
    paragraphs.forEach((paragraph) => {
      const text = String(paragraph?.text || "");
      if (!text.trim()) return;
      const seen = new Set();
      blankMatches(text).forEach((match) => {
        if (seen.has(match.index)) return;
        seen.add(match.index);
        const slot = classifyBlank(text, match.index, match.text);
        if (!slot) return;
        out.push({
          id: `ins-${paragraph.id}-${match.index}`,
          mode: "insert",
          slot,
          paragraph_id: String(paragraph.id),
          paragraph_index: paragraph.index ?? null,
          find: match.text,
          offset: match.index,
          context: text,
        });
      });
    });
    return out;
  }

  // ── Detection: REPLACE (an Aspora entity already present in the document) ────
  // Case-insensitive raw-substring locate so the `find` we store is the exact text
  // present in the paragraph (clean-fill rewrites by indexOf(find)).
  function rawOccurrence(text, needle) {
    const n = String(needle || "");
    if (!n) return null;
    const idx = text.toLowerCase().indexOf(n.toLowerCase());
    if (idx === -1) return null;
    return { index: idx, raw: text.slice(idx, idx + n.length) };
  }

  // (a) Exact match: an entity's verbatim registry name/address is in the document.
  function detectExactReplacements() {
    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    const targetId = (targetEntity() || {}).id || null;
    const entities = registryEntities();
    const out = [];
    paragraphs.forEach((paragraph) => {
      const text = String(paragraph?.text || "");
      if (!text.trim()) return;
      entities.forEach((entity) => {
        if (entity.id === targetId) return; // already the chosen entity — nothing to swap
        const legal = String(entity.legal_name || "").trim();
        const nameHit = rawOccurrence(text, legal);
        if (nameHit) {
          out.push({
            id: `rep-name-${paragraph.id}-${entity.id}-${nameHit.index}`,
            mode: "replace",
            slot: "name",
            paragraph_id: String(paragraph.id),
            paragraph_index: paragraph.index ?? null,
            find: nameHit.raw,
            offset: nameHit.index,
            context: text,
            sourceLabel: entity.short_name || legal,
          });
        }
        entityAddressStrings(entity).forEach((addr) => {
          const addrHit = rawOccurrence(text, addr);
          if (addrHit) {
            out.push({
              id: `rep-addr-${paragraph.id}-${entity.id}-${addrHit.index}`,
              mode: "replace",
              slot: "address",
              paragraph_id: String(paragraph.id),
              paragraph_index: paragraph.index ?? null,
              find: addrHit.raw,
              offset: addrHit.index,
              context: text,
              sourceLabel: entity.short_name || legal,
            });
          }
        });
      });
    });
    return out;
  }

  function escapeRe(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Defined-terms that mark OUR side in counterparty paper: the "Aspora" brand plus
  // each registry entity's short name (e.g. "Vance Money").
  function brandAliases() {
    const names = ["Aspora"].concat(
      registryEntities().map((entity) => String(entity.short_name || "").trim()).filter(Boolean),
    );
    return [...new Set(names.map((name) => name.toLowerCase()))];
  }

  // (b) Alias match: a party clause whose DEFINED TERM is one of our brands (e.g.
  // 'Vance Inc. ... having its registered office at X ... referred to as "Aspora"').
  // The party name itself need not be a verbatim registry name — the alias marks it
  // as our side. We extract the name + registered office to offer a swap.
  function detectAliasReplacements() {
    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    const aliases = brandAliases();
    const out = [];
    paragraphs.forEach((paragraph) => {
      const text = String(paragraph?.text || "");
      if (!text.trim()) return;
      const aliased = aliases.some((alias) =>
        new RegExp(`(?:referred to as\\s*|\\(\\s*)["“']?\\s*${escapeRe(alias)}\\b`, "i").test(text));
      if (!aliased) return;

      // NAME: the entity name at the clause start, up to ", a/an company", ", having", etc.
      const nameMatch = text.match(/^\s*([A-Z][^,\n]{1,90}?)\s*,\s*(?:an? |incorporated|having|with its|registered)/);
      if (nameMatch) {
        const hit = rawOccurrence(text, nameMatch[1].trim());
        if (hit) {
          out.push({
            id: `rep-alias-name-${paragraph.id}-${hit.index}`,
            mode: "replace",
            slot: "name",
            paragraph_id: String(paragraph.id),
            paragraph_index: paragraph.index ?? null,
            find: hit.raw,
            offset: hit.index,
            context: text,
            sourceLabel: hit.raw,
          });
        }
      }
      // ADDRESS: after "registered office at …", up to "hereinafter"/"referred to"/"(".
      const addrMatch = text.match(/(?:registered office (?:at|located at)|having its (?:registered )?office (?:at\s+)?|office (?:at|located at)|principal place of business at)\s+([^]+?)\s*,?\s*(?:hereinafter|referred to as|\()/i);
      if (addrMatch) {
        const hit = rawOccurrence(text, addrMatch[1].trim());
        if (hit && hit.raw.length > 6) {
          out.push({
            id: `rep-alias-addr-${paragraph.id}-${hit.index}`,
            mode: "replace",
            slot: "address",
            paragraph_id: String(paragraph.id),
            paragraph_index: paragraph.index ?? null,
            find: hit.raw,
            offset: hit.index,
            context: text,
            sourceLabel: "current registered office",
          });
        }
      }
    });
    return out;
  }

  function detectReplacements() {
    const combined = detectExactReplacements().concat(detectAliasReplacements());
    const seen = new Set();
    return combined.filter((item) => {
      const key = `${item.paragraph_id}|${item.slot}|${item.offset}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function workingFor(candidate) {
    let existing = itemState.get(candidate.id);
    if (!existing) {
      existing = { mode: "clean", enabled: true };
      itemState.set(candidate.id, existing);
    }
    return existing;
  }

  // ── Render ──────────────────────────────────────────────────────────────────
  function render() {
    if (!root) return;
    ensureRegistry();
    api();

    const paragraphs = Array.isArray(state.reviewParagraphs) ? state.reviewParagraphs : [];
    if (!paragraphs.length) {
      clearDocHighlights();
      root.innerHTML = '<div class="fill-empty">Load or review an NDA to scan it for Aspora name &amp; address slots.</div>';
      return;
    }

    const inserts = detectInserts();
    const replacements = detectReplacements();
    const hasTarget = Boolean(targetEntity());

    root.innerHTML = `
      ${renderEntityPicker()}
      ${renderAppliedSummary()}
      ${renderSection("Insert into blanks", inserts)}
      ${renderSection("Replace existing Aspora identity", replacements)}
      ${(inserts.length || replacements.length) ? renderActions() : '<div class="fill-empty">No name or address blanks — and no existing Aspora identity — found in this document.</div>'}
    `;
    const candidates = inserts.concat(replacements);
    bindControls(candidates);
    decorateDocument(candidates);
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
        </label>`;
    }
    const bundle = entity
      ? `<dl class="fill-bundle-grid">
          <div><dt>Name</dt><dd>${escape(targetName() || "—")}</dd></div>
          <div><dt>Registered office</dt><dd>${escape(targetAddress() || "—")}</dd></div>
        </dl>`
      : '<p class="fill-bundle-empty">Pick the Aspora entity to insert / replace with.</p>';
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
      </section>`;
  }

  function renderSection(title, candidates) {
    if (!candidates.length) return "";
    return `
      <section class="fill-section" aria-label="${escape(title)}">
        <h3 class="fill-section-title">${escape(title)} (${candidates.length})</h3>
        ${candidates.map(renderRow).join("")}
      </section>`;
  }

  function renderRow(candidate) {
    const work = workingFor(candidate);
    const tracked = work.mode === "tracked";
    const value = valueForSlot(candidate.slot);
    const slotLabel = candidate.slot === "address" ? "Address" : "Name";
    const para = candidate.paragraph_index != null ? `¶${escape(candidate.paragraph_index)} · ` : "";
    const head = candidate.mode === "replace"
      ? `<span class="fill-slot">Replace ${escape(slotLabel.toLowerCase())}</span>`
      : `<span class="fill-slot">${escape(slotLabel)}</span>`;
    const valueLine = value
      ? `<span class="fill-row-value">${escape(value)}</span>`
      : '<span class="fill-row-value muted">pick an entity above</span>';
    return `
      <article class="fill-blank-row${work.enabled ? "" : " disabled"}" data-fill-id="${escape(candidate.id)}">
        <header class="fill-blank-head">
          <label class="fill-blank-enable">
            <input type="checkbox" data-fill-enable${work.enabled ? " checked" : ""}>
            <span>${para}${head}</span>
          </label>
        </header>
        <p class="fill-blank-context">${renderContext(candidate)}</p>
        <div class="fill-blank-controls">
          <span class="fill-arrow" aria-hidden="true">→</span>
          ${valueLine}
          <div class="fill-mode-toggle" role="group" aria-label="Fill mode">
            <button type="button" data-fill-mode="clean" class="${tracked ? "" : "active"}" aria-pressed="${tracked ? "false" : "true"}">Clean</button>
            <button type="button" data-fill-mode="tracked" class="${tracked ? "active" : ""}" aria-pressed="${tracked ? "true" : "false"}">Tracked</button>
          </div>
        </div>
      </article>`;
  }

  function renderContext(candidate) {
    const text = String(candidate.context || "");
    const start = Number(candidate.offset) || 0;
    const end = start + String(candidate.find || "").length;
    const head = clip(text.slice(0, start), -80);
    const tail = clip(text.slice(end), 80);
    return `${escape(head)}<mark class="fill-blank-mark">${escape(candidate.find)}</mark>${escape(tail)}`;
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
        <button type="button" class="fill-apply" data-fill-apply>Apply</button>
      </div>`;
  }

  function renderAppliedSummary() {
    const fills = Array.isArray(state.filledBlanks) ? state.filledBlanks : [];
    if (!fills.length) return "";
    const cleanCount = fills.filter((fill) => fill.mode === "clean").length;
    const trackedCount = fills.length - cleanCount;
    return `
      <div class="fill-applied" role="status">
        ${escape(fills.length)} applied (${escape(cleanCount)} clean, ${escape(trackedCount)} tracked).
        <button type="button" class="fill-clear" data-fill-clear>Clear</button>
      </div>`;
  }

  // ── Events ──────────────────────────────────────────────────────────────────
  function bindControls(candidates) {
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

    const byId = new Map(candidates.map((candidate) => [candidate.id, candidate]));
    root.querySelectorAll("[data-fill-id]").forEach((rowNode) => {
      const candidate = byId.get(rowNode.dataset.fillId);
      if (!candidate) return;
      const work = workingFor(candidate);
      const enable = rowNode.querySelector("[data-fill-enable]");
      enable?.addEventListener("change", () => {
        work.enabled = enable.checked;
        rowNode.classList.toggle("disabled", !work.enabled);
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

    root.querySelector("[data-fill-apply]")?.addEventListener("click", () => applyFills(candidates));
    root.querySelector("[data-fill-clear]")?.addEventListener("click", () => clearFills());
  }

  // ── Apply ─────────────────────────────────────────────────────────────────
  // Each enabled candidate becomes a fill record { paragraph_id, find, value,
  // mode }. CLEAN rewrites the paragraph text + advances the manual-redline
  // baseline (so manualExportRedlines doesn't double-emit it); TRACKED leaves the
  // text alone for the backend to render as a tracked change on export.
  function applyFills(candidates) {
    api();
    let cleanTouched = false;
    let applied = 0;
    candidates.forEach((candidate) => {
      const work = workingFor(candidate);
      if (!work.enabled) return;
      const value = String(valueForSlot(candidate.slot) || "").trim();
      if (!value || value === candidate.find) return;
      const record = {
        id: candidate.id,
        paragraph_id: candidate.paragraph_id,
        find: candidate.find,
        value,
        field: candidate.slot === "address" ? "registered_office" : "legal_name",
        mode: work.mode === "tracked" ? "tracked" : "clean",
      };
      upsertFill(record);
      applied += 1;
      if (record.mode === "clean" && applyCleanFill(record)) cleanTouched = true;
    });

    if (cleanTouched && typeof rerenderDocument === "function") rerenderDocument();
    if (typeof markRedlineDraftDirty === "function") markRedlineDraftDirty();
    render();
    if (typeof setFileMeta === "function") {
      const name = targetName();
      setFileMeta(applied
        ? `Applied ${applied} ${applied === 1 ? "change" : "changes"}${name ? ` for ${name}` : ""}.`
        : "Nothing applied — pick an entity and enable a row.");
    }
  }

  // Rewrites the first occurrence of `find` in the paragraph with `value`, in BOTH
  // the live paragraph and the export baselines, so the viewer shows the change and
  // manualExportRedlines() sees no diff for it.
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

  // ── Document highlighting ───────────────────────────────────────────────────
  // Mirrors the generator's yellow placeholder highlight: marks each detected slot
  // / existing-identity span directly in the rendered document so the reviewer sees
  // WHERE each row points. Post-render DOM decoration (fully unwrappable), re-applied
  // on every Fill render and cleared when the Fill tab is left.
  function cssEscape(value) {
    if (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(String(value));
    }
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  function clearDocHighlights() {
    const render = document.getElementById("studioDocumentRender");
    if (!render) return;
    render.querySelectorAll("mark.fill-doc-highlight").forEach((mark) => {
      const parent = mark.parentNode;
      if (!parent) return;
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
      parent.removeChild(mark);
      parent.normalize();
    });
  }

  function highlightSpan(el, find) {
    const needle = String(find || "");
    if (!needle) return;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    let node = walker.nextNode();
    while (node) {
      const parent = node.parentNode;
      const alreadyMarked = parent && parent.classList && parent.classList.contains("fill-doc-highlight");
      if (!alreadyMarked) {
        const idx = node.nodeValue.indexOf(needle);
        if (idx !== -1) {
          const range = document.createRange();
          range.setStart(node, idx);
          range.setEnd(node, idx + needle.length);
          const mark = document.createElement("mark");
          mark.className = "fill-doc-highlight";
          mark.setAttribute("contenteditable", "false");
          try {
            range.surroundContents(mark);
          } catch (error) {
            // The span crosses element boundaries (rich runs) — skip rather than break.
          }
          return;
        }
      }
      node = walker.nextNode();
    }
  }

  function decorateDocument(candidates) {
    const render = document.getElementById("studioDocumentRender");
    if (!render || render.hidden) return;
    clearDocHighlights();
    candidates.forEach((candidate) => {
      const id = String(candidate.paragraph_id || "");
      const para = render.querySelector(`[data-editable-paragraph-id="${cssEscape(id)}"]`)
        || render.querySelector(`[data-paragraph-id="${cssEscape(id)}"]`);
      if (para) highlightSpan(para, candidate.find);
    });
  }

  return { render, clearHighlights: clearDocHighlights };
}

// Export payload helper shared by the DOCX export (and send-redline). Maps
// state.filledBlanks to the backend shape { paragraph_id, find, value, mode }. The
// backend substitutes find->value as plain text (clean) or a tracked change.
function currentReviewFills() {
  const fills = Array.isArray(state.filledBlanks) ? state.filledBlanks : [];
  return fills.map((fill) => ({
    paragraph_id: fill.paragraph_id,
    find: fill.find,
    value: fill.value,
    mode: fill.mode === "tracked" ? "tracked" : "clean",
  }));
}
